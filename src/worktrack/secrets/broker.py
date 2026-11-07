"""Stateless OAuth 2.0 PKCE Confidential Token-Exchange Broker (`PROB-WT-001`)."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterable, Mapping
from typing import NoReturn

from worktrack.secrets.exceptions import (
    OAuthBrokerError,
    OAuthInvalidClientError,
    SecretError,
    SecretValue,
)
from worktrack.secrets.oauth_http import (
    DEFAULT_PKCE_VERIFIER_BYTES,
    DEFAULT_TOKEN_TYPE,
    PKCE_CODE_CHALLENGE_METHOD_S256,
    OAuthHttpResponse,
    OAuthHttpTransport,
    OAuthProviderConfig,
    OAuthTokenBundle,
    PkceChallenge,
    UrllibOAuthHttpTransport,
    compute_pkce_s256_challenge,
    generate_pkce_pair,
)
from worktrack.secrets.providers import OUTCOME_ERROR, OUTCOME_OK, SecretResolver
from worktrack.telemetry.logging import StructuredEventLogger
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
DEFAULT_OAUTH_HTTP_TIMEOUT_SECONDS: float = 10.0
MIN_POSITIVE_TIMEOUT_SECONDS: float = 0.0
ENV_OAUTH_HTTP_TIMEOUT_SECONDS = "WORKTRACK_OAUTH_HTTP_TIMEOUT_SECONDS"

GRANT_AUTHORIZATION_CODE = "authorization_code"
GRANT_REFRESH_TOKEN = "refresh_token"
OUTCOME_ROTATED_OK = "rotated_ok"
UNKNOWN_PROVIDER_LABEL = "unknown"
OAUTH_ERROR_INVALID_CLIENT = "invalid_client"
EVENT_OAUTH_BROKER_EXCHANGE = "oauth.broker.exchange"

HTTP_SUCCESS_MIN = 200
HTTP_SUCCESS_MAX = 299

__all__ = [
    "DEFAULT_OAUTH_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_PKCE_VERIFIER_BYTES",
    "DEFAULT_TOKEN_TYPE",
    "GRANT_AUTHORIZATION_CODE",
    "GRANT_REFRESH_TOKEN",
    "OUTCOME_ROTATED_OK",
    "PKCE_CODE_CHALLENGE_METHOD_S256",
    "OAuthHttpResponse",
    "OAuthHttpTransport",
    "OAuthProviderConfig",
    "OAuthTokenBundle",
    "PkceChallenge",
    "StatelessOAuthBroker",
    "UrllibOAuthHttpTransport",
    "compute_pkce_s256_challenge",
    "generate_pkce_pair",
]
# endregion


# region Broker
class StatelessOAuthBroker:
    """Stateless confidential OAuth 2.0 PKCE token-exchange and refresh broker.

    Resolves application `client_secret` via `SecretResolver`, automatically evicts
    and retries once upon upstream `invalid_client` secret rotation, and retains
    zero user tokens or secrets in memory, disk, logs, or Prometheus labels.
    """

    def __init__(
        self,
        *,
        secret_resolver: SecretResolver,
        providers: Iterable[OAuthProviderConfig],
        transport: OAuthHttpTransport | None = None,
        timeout_seconds: float | None = None,
        telemetry: TelemetryRecorder | None = None,
        event_logger: StructuredEventLogger | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize stateless OAuth broker with providers and secret resolver."""
        resolved_timeout = _resolve_http_timeout(timeout_seconds, environ)
        self._secret_resolver = secret_resolver
        self._providers: dict[str, OAuthProviderConfig] = {cfg.provider: cfg for cfg in providers}
        self._transport: OAuthHttpTransport = (
            UrllibOAuthHttpTransport() if transport is None else transport
        )
        self._timeout_seconds = resolved_timeout
        self._telemetry = telemetry
        self._event_logger = event_logger

    def exchange_authorization_code(
        self,
        provider: str,
        code: str,
        code_verifier: str | SecretValue,
        redirect_uri: str,
    ) -> OAuthTokenBundle:
        """Exchange an authorization code + PKCE `code_verifier` for an `OAuthTokenBundle`."""
        verifier_str = (
            code_verifier.expose() if isinstance(code_verifier, SecretValue) else code_verifier
        )
        if not code.strip() or not redirect_uri.strip():
            self._record_outcome(provider, GRANT_AUTHORIZATION_CODE, OUTCOME_ERROR)
            raise OAuthBrokerError(
                "authorization_code exchange requires non-empty code and redirect_uri.",
                redact=(code, verifier_str),
            )
        compute_pkce_s256_challenge(verifier_str)
        return self._execute_grant(
            provider=provider,
            grant_type=GRANT_AUTHORIZATION_CODE,
            grant_params={
                "code": code,
                "code_verifier": verifier_str,
                "redirect_uri": redirect_uri,
            },
            redact_values=(code, verifier_str),
        )

    def refresh_access_token(
        self,
        provider: str,
        refresh_token: str | SecretValue,
    ) -> OAuthTokenBundle:
        """Exchange a `refresh_token` for a fresh `OAuthTokenBundle`."""
        refresh_str = (
            refresh_token.expose() if isinstance(refresh_token, SecretValue) else refresh_token
        )
        if not refresh_str.strip():
            self._record_outcome(provider, GRANT_REFRESH_TOKEN, OUTCOME_ERROR)
            raise OAuthBrokerError("refresh_token grant requires a non-empty refresh_token.")
        return self._execute_grant(
            provider=provider,
            grant_type=GRANT_REFRESH_TOKEN,
            grant_params={"refresh_token": refresh_str},
            redact_values=(refresh_str,),
        )

    def _execute_grant(
        self,
        *,
        provider: str,
        grant_type: str,
        grant_params: Mapping[str, str],
        redact_values: tuple[str, ...],
    ) -> OAuthTokenBundle:
        """Resolve `client_secret`, invoke token endpoint, and retry once on rotation."""
        config = self._require_provider(provider, grant_type, redact_values)
        client_secret = self._resolve_client_secret(config, grant_type, redact_values)
        response = self._post_grant(config, grant_type, grant_params, client_secret)
        outcome = OUTCOME_OK
        active_secrets: list[str | SecretValue] = [*redact_values, client_secret]

        if _is_invalid_client(response):
            self._secret_resolver.invalidate(config.client_secret_uri)
            rotated = self._resolve_client_secret(config, grant_type, redact_values)
            active_secrets.append(rotated)
            response = self._post_grant(config, grant_type, grant_params, rotated)
            outcome = OUTCOME_ROTATED_OK

        if not (HTTP_SUCCESS_MIN <= response.status_code <= HTTP_SUCCESS_MAX):
            self._record_outcome(config.provider, grant_type, OUTCOME_ERROR)
            _raise_upstream_oauth_error(config.provider, response, active_secrets)

        bundle = _parse_token_bundle(
            response.payload,
            active_secrets,
            self._fail_hook(config.provider, grant_type),
        )
        self._record_outcome(config.provider, grant_type, outcome)
        return bundle

    def _fail_hook(self, provider: str, grant_type: str) -> Callable[[], None]:
        """Create callback recording error outcome when token response schema is invalid."""

        def _on_error() -> None:
            """Record OAuth broker error outcome on invalid upstream payload."""
            self._record_outcome(provider, grant_type, OUTCOME_ERROR)

        return _on_error

    def _require_provider(
        self,
        provider: str,
        grant_type: str,
        redact_values: tuple[str, ...],
    ) -> OAuthProviderConfig:
        """Look up registered `OAuthProviderConfig` or record bounded error."""
        config = self._providers.get(provider)
        if config is None:
            self._record_outcome(UNKNOWN_PROVIDER_LABEL, grant_type, OUTCOME_ERROR)
            raise OAuthBrokerError(
                f"Unregistered OAuth provider '{provider}'.",
                redact=redact_values,
            )
        return config

    def _resolve_client_secret(
        self,
        config: OAuthProviderConfig,
        grant_type: str,
        redact_values: tuple[str, ...],
    ) -> SecretValue:
        """Resolve `config.client_secret_uri` and record broker failure if unavailable."""
        try:
            return self._secret_resolver.resolve(config.client_secret_uri)
        except SecretError as exc:
            self._record_outcome(config.provider, grant_type, OUTCOME_ERROR)
            raise OAuthBrokerError(
                f"Failed to resolve client_secret for provider '{config.provider}': {exc}",
                redact=redact_values,
            ) from exc

    def _post_grant(
        self,
        config: OAuthProviderConfig,
        grant_type: str,
        grant_params: Mapping[str, str],
        client_secret: SecretValue,
    ) -> OAuthHttpResponse:
        """Submit OAuth token POST with confidential client credentials."""
        form = {
            **grant_params,
            "grant_type": grant_type,
            "client_id": config.client_id,
            "client_secret": client_secret.expose(),
        }
        try:
            return self._transport.post_form(
                config.token_endpoint,
                form,
                timeout_seconds=self._timeout_seconds,
            )
        except OAuthBrokerError:
            self._record_outcome(config.provider, grant_type, OUTCOME_ERROR)
            raise
        except Exception as exc:
            self._record_outcome(config.provider, grant_type, OUTCOME_ERROR)
            raise OAuthBrokerError(
                f"Transport error talking to OAuth provider '{config.provider}'.",
                redact=tuple(form.values()),
            ) from exc

    def _record_outcome(self, provider: str, grant_type: str, outcome: str) -> None:
        """Record bounded Prometheus counter and redacted JSONL trace event."""
        safe_provider = provider if provider in self._providers else UNKNOWN_PROVIDER_LABEL
        if self._telemetry is not None:
            self._telemetry.record_oauth_broker_request(
                provider=safe_provider,
                grant=grant_type,
                outcome=outcome,
            )
        if self._event_logger is not None:
            self._event_logger.emit(
                EVENT_OAUTH_BROKER_EXCHANGE,
                provider=safe_provider,
                grant=grant_type,
                outcome=outcome,
            )


# endregion


# region Validation
def _resolve_http_timeout(
    timeout_seconds: float | None,
    environ: Mapping[str, str] | None,
) -> float:
    """Resolve and validate OAuth HTTP timeout from explicit arg or environment."""
    if timeout_seconds is None:
        env = os.environ if environ is None else environ
        raw = env.get(ENV_OAUTH_HTTP_TIMEOUT_SECONDS, "").strip()
        timeout_seconds = float(raw) if raw else DEFAULT_OAUTH_HTTP_TIMEOUT_SECONDS
    if not math.isfinite(timeout_seconds) or timeout_seconds <= MIN_POSITIVE_TIMEOUT_SECONDS:
        raise OAuthBrokerError("OAuth HTTP timeout_seconds must be finite and > 0.")
    return timeout_seconds


def _is_invalid_client(response: OAuthHttpResponse) -> bool:
    """Return True if OAuth response signals `invalid_client` credential failure."""
    return response.payload.get("error") == OAUTH_ERROR_INVALID_CLIENT


def _raise_upstream_oauth_error(
    provider: str,
    response: OAuthHttpResponse,
    redact_secrets: list[str | SecretValue],
) -> NoReturn:
    """Raise scrubbed `OAuthInvalidClientError` or `OAuthBrokerError` for failed response."""
    err_code = str(response.payload.get("error", "oauth_error"))
    raw_desc = response.payload.get("error_description")
    desc_suffix = f" ({raw_desc})" if isinstance(raw_desc, str) and raw_desc.strip() else ""
    message = (
        f"OAuth token request for '{provider}' failed with HTTP {response.status_code}: "
        f"{err_code}{desc_suffix}"
    )
    if err_code == OAUTH_ERROR_INVALID_CLIENT:
        raise OAuthInvalidClientError(message, redact=redact_secrets)
    raise OAuthBrokerError(message, redact=redact_secrets)


def _parse_token_bundle(
    payload: Mapping[str, object],
    redact_secrets: list[str | SecretValue],
    on_error: Callable[[], None],
) -> OAuthTokenBundle:
    """Validate OAuth token JSON fields and wrap tokens in `SecretValue`."""
    raw_access = _require_non_empty_str(payload.get("access_token"))
    if raw_access is None:
        on_error()
        raise OAuthBrokerError(
            "Upstream OAuth response omitted a valid 'access_token'.",
            redact=redact_secrets,
        )

    raw_refresh = _require_non_empty_str(payload.get("refresh_token"))
    expires_in = _parse_optional_expires_in(
        payload.get("expires_in"),
        [*redact_secrets, raw_access],
        on_error,
    )
    token_type = _require_non_empty_str(payload.get("token_type")) or DEFAULT_TOKEN_TYPE
    raw_scope = payload.get("scope")
    return OAuthTokenBundle(
        access_token=SecretValue(raw_access),
        refresh_token=SecretValue(raw_refresh) if raw_refresh is not None else None,
        expires_in=expires_in,
        token_type=token_type,
        scope=raw_scope if isinstance(raw_scope, str) else None,
    )


def _require_non_empty_str(candidate: object) -> str | None:
    """Return `candidate` when it is a non-empty string, else `None`."""
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    return None


def _parse_optional_expires_in(
    raw_expires: object,
    redact_secrets: list[str | SecretValue],
    on_error: Callable[[], None],
) -> int | None:
    """Validate optional integer `expires_in` field from OAuth token payload."""
    if raw_expires is None:
        return None
    if isinstance(raw_expires, bool) or not isinstance(raw_expires, int):
        on_error()
        raise OAuthBrokerError(
            "Upstream OAuth 'expires_in' field must be an integer.",
            redact=redact_secrets,
        )
    return raw_expires


# endregion
