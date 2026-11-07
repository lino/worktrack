"""Jira Cloud OAuth 3LO client, local token refresh, and `urllib.request` transport."""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from worktrack.connectors.exceptions import (
    JiraApiError,
    JiraAuthenticationError,
    JiraConfigurationError,
    JiraTransportError,
)
from worktrack.secrets import (
    DEFAULT_TOKEN_ACCOUNT,
    OAuthTokenBundle,
    SecretError,
    SecretValue,
    StatelessOAuthBroker,
    UserTokenStore,
)

# region Constants
JIRA_PROVIDER_SLUG: Final = "jira"
DEFAULT_ATLASSIAN_API_ORIGIN: Final = "https://api.atlassian.com"
ENV_JIRA_API_BASE_URL: Final = "WORKTRACK_JIRA_API_BASE_URL"
ENV_JIRA_CLOUD_ID: Final = "WORKTRACK_JIRA_CLOUD_ID"
ENV_JIRA_HTTP_TIMEOUT_SECONDS: Final = "WORKTRACK_JIRA_HTTP_TIMEOUT_SECONDS"

DEFAULT_JIRA_HTTP_TIMEOUT_SECONDS: Final = 15.0
MIN_POSITIVE_TIMEOUT_SECONDS: Final = 0.0
DEFAULT_TOKEN_EXPIRY_SKEW_SECONDS: Final = 60

HTTP_STATUS_OK_MIN: Final = 200
HTTP_STATUS_OK_MAX: Final = 299
HTTP_STATUS_UNAUTHORIZED: Final = 401

HTTPS_SCHEME: Final = "https"
HTTP_SCHEME: Final = "http"
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
JSON_CONTENT_TYPE: Final = "application/json"

type TokenRefresher = Callable[[SecretValue], OAuthTokenBundle]
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class JiraCloudConfig:
    """Configuration for Jira Cloud REST API v3 OAuth 3LO connectivity."""

    cloud_id: str | None = None
    api_base_url: str | None = None
    provider_name: str = JIRA_PROVIDER_SLUG
    account: str = DEFAULT_TOKEN_ACCOUNT
    timeout_seconds: float | None = None
    expiry_skew_seconds: int = DEFAULT_TOKEN_EXPIRY_SKEW_SECONDS

    def resolve_base_url(self, environ: Mapping[str, str] | None = None) -> str:
        """Resolve and validate Jira Cloud REST base URL from config or environment."""
        env = os.environ if environ is None else environ
        explicit_url = (self.api_base_url or env.get(ENV_JIRA_API_BASE_URL, "")).strip()
        if explicit_url:
            cleaned = explicit_url.rstrip("/")
            validate_jira_url(cleaned)
            return cleaned
        resolved_cloud = (self.cloud_id or env.get(ENV_JIRA_CLOUD_ID, "")).strip()
        if not resolved_cloud:
            raise JiraConfigurationError(
                f"Provide cloud_id, api_base_url, {ENV_JIRA_CLOUD_ID}, or {ENV_JIRA_API_BASE_URL}."
            )
        encoded_cloud = urllib.parse.quote(resolved_cloud, safe="")
        built = f"{DEFAULT_ATLASSIAN_API_ORIGIN}/ex/jira/{encoded_cloud}"
        validate_jira_url(built)
        return built

    def resolve_timeout(self, environ: Mapping[str, str] | None = None) -> float:
        """Resolve finite positive HTTP timeout in seconds."""
        if self.timeout_seconds is not None:
            timeout = self.timeout_seconds
        else:
            env = os.environ if environ is None else environ
            raw = env.get(ENV_JIRA_HTTP_TIMEOUT_SECONDS, "").strip()
            timeout = float(raw) if raw else DEFAULT_JIRA_HTTP_TIMEOUT_SECONDS
        if not math.isfinite(timeout) or timeout <= MIN_POSITIVE_TIMEOUT_SECONDS:
            raise JiraConfigurationError("Jira HTTP timeout_seconds must be finite and > 0.")
        return timeout


@dataclass(frozen=True, slots=True)
class JiraHttpResponse:
    """Decoded HTTP status, JSON body, and response headers from Jira Cloud."""

    status_code: int
    payload: Mapping[str, object]
    headers: Mapping[str, str] = field(default_factory=dict)


# endregion


# region Protocols
class JiraHttpTransport(Protocol):
    """Injectable HTTP transport protocol for Jira Cloud REST API v3 requests."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float = DEFAULT_JIRA_HTTP_TIMEOUT_SECONDS,
    ) -> JiraHttpResponse:
        """Execute an HTTP request and return decoded `JiraHttpResponse`."""
        ...


# endregion


# region HTTP Transport
def validate_jira_url(url: str) -> None:
    """Require `https://` for remote Jira hosts or loopback `http://` for local tests."""
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme == HTTPS_SCHEME and host:
        return
    if parsed.scheme == HTTP_SCHEME and host in LOOPBACK_HOSTS:
        return
    raise JiraConfigurationError(
        "Jira API URL must use 'https://' (or loopback 'http://127.0.0.1' in local tests)."
    )


class UrllibJiraHttpTransport:
    """Default stdlib `urllib.request` implementation of `JiraHttpTransport`."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float = DEFAULT_JIRA_HTTP_TIMEOUT_SECONDS,
    ) -> JiraHttpResponse:
        """Execute HTTP request via `urllib.request` and decode JSON response safely."""
        validate_jira_url(url)
        body_bytes = None if json_body is None else json.dumps(json_body).encode("utf-8")
        req_headers = {"Accept": JSON_CONTENT_TYPE, **dict(headers)}
        if body_bytes is not None:
            req_headers.setdefault("Content-Type", JSON_CONTENT_TYPE)
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers=req_headers,
            method=method.upper(),
        )
        redact_vals = tuple(v for k, v in req_headers.items() if k.lower() == "authorization")
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw = resp.read()
                resp_headers = dict(resp.headers.items())
                return JiraHttpResponse(
                    status_code=int(resp.status),
                    payload=_decode_jira_json(raw, redact_vals, url=url, allow_empty=True),
                    headers=resp_headers,
                )
        except urllib.error.HTTPError as exc:
            err_raw = exc.read()
            err_headers = dict(exc.headers.items()) if exc.headers is not None else {}
            payload = _decode_jira_json(
                err_raw, redact_vals, url=url, allow_empty=True, allow_non_json=True
            )
            return JiraHttpResponse(
                status_code=int(exc.code),
                payload=payload,
                headers=err_headers,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise JiraTransportError(
                f"Jira HTTP transport error calling '{url}'.",
                destination_uri=url,
                redact=redact_vals,
            ) from exc


def _decode_jira_json(
    raw_bytes: bytes,
    redact_vals: tuple[str, ...],
    *,
    url: str,
    allow_empty: bool = False,
    allow_non_json: bool = False,
) -> Mapping[str, object]:
    """Parse UTF-8 JSON object from Jira HTTP response bytes."""
    if not raw_bytes.strip() and allow_empty:
        return {}
    text = raw_bytes.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        if allow_non_json:
            return {"errorMessages": [text]}
        raise JiraTransportError(
            "Jira Cloud endpoint returned invalid JSON.",
            destination_uri=url,
            redact=(*redact_vals, text),
        ) from exc
    if isinstance(parsed, Mapping):
        return parsed
    return {"items": parsed}


# endregion


# region OAuth Client
class JiraOAuthClient:
    """Workstation-local Jira Cloud OAuth 3LO client with transparent token refresh."""

    def __init__(
        self,
        *,
        config: JiraCloudConfig,
        token_store: UserTokenStore,
        token_refresher: TokenRefresher | None = None,
        oauth_broker: StatelessOAuthBroker | None = None,
        transport: JiraHttpTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize client with local `UserTokenStore` and optional refresh broker."""
        self._config = config
        self._base_url = config.resolve_base_url(environ=environ)
        self._timeout = config.resolve_timeout(environ=environ)
        self._token_store = token_store
        self._refresher = _resolve_refresher(config.provider_name, token_refresher, oauth_broker)
        self._transport: JiraHttpTransport = (
            UrllibJiraHttpTransport() if transport is None else transport
        )
        self._clock = clock
        self._cached_bundle: OAuthTokenBundle | None = None
        self._acquired_at: float | None = None

    @property
    def base_url(self) -> str:
        """Return resolved Jira Cloud REST base URL."""
        return self._base_url

    def build_url(self, path: str) -> str:
        """Join relative Jira REST `path` onto `base_url`."""
        rel = path if path.startswith("/") else f"/{path}"
        return f"{self._base_url}{rel}"

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> JiraHttpResponse:
        """Execute authenticated Jira REST v3 call with near-expiry and `401` refresh."""
        url = self.build_url(path)
        bundle, secrets_seen = self._get_ready_bundle()
        response = self._send_with_bearer(method, url, bundle, json_body, extra_headers)

        if response.status_code == HTTP_STATUS_UNAUTHORIZED and bundle.refresh_token is not None:
            bundle = self._refresh_and_persist(bundle, secrets_seen)
            _collect_bundle_secrets(bundle, secrets_seen)
            response = self._send_with_bearer(method, url, bundle, json_body, extra_headers)

        if response.status_code == HTTP_STATUS_UNAUTHORIZED:
            raise JiraAuthenticationError(
                f"Jira Cloud rejected bearer token with HTTP 401 at '{path}'.",
                http_status=HTTP_STATUS_UNAUTHORIZED,
                destination_uri=url,
                redact=secrets_seen,
            )
        if not (HTTP_STATUS_OK_MIN <= response.status_code <= HTTP_STATUS_OK_MAX):
            detail = _extract_jira_error_summary(response.payload)
            raise JiraApiError(
                f"Jira API {method.upper()} {path} failed "
                f"with HTTP {response.status_code}: {detail}",
                http_status=response.status_code,
                destination_uri=url,
                redact=secrets_seen,
            )
        return response

    def _get_ready_bundle(self) -> tuple[OAuthTokenBundle, list[str | SecretValue]]:
        """Load token bundle from `UserTokenStore` and proactively refresh if near expiry."""
        bundle = self._load_bundle()
        secrets_seen: list[str | SecretValue] = []
        _collect_bundle_secrets(bundle, secrets_seen)
        if (
            self._is_near_expiry(bundle)
            and bundle.refresh_token is not None
            and self._refresher is not None
        ):
            bundle = self._refresh_and_persist(bundle, secrets_seen)
            _collect_bundle_secrets(bundle, secrets_seen)
        return bundle, secrets_seen

    def _load_bundle(self) -> OAuthTokenBundle:
        """Load user token from local `UserTokenStore` or raise `JiraAuthenticationError`."""
        try:
            stored = self._token_store.load(self._config.provider_name, self._config.account)
        except SecretError as exc:
            raise JiraAuthenticationError(f"Cannot load local Jira user token: {exc}") from exc
        if stored is None:
            raise JiraAuthenticationError(
                "No Jira OAuth token found in local UserTokenStore "
                f"for '{self._config.provider_name}'."
            )
        if self._cached_bundle != stored or self._acquired_at is None:
            self._cached_bundle = stored
            self._acquired_at = self._clock()
        return stored

    def _is_near_expiry(self, bundle: OAuthTokenBundle) -> bool:
        """Return `True` when `bundle` is within `expiry_skew_seconds` of expiration."""
        if bundle.expires_in is None:
            return False
        acquired = self._clock() if self._acquired_at is None else self._acquired_at
        elapsed = max(0.0, self._clock() - acquired)
        remaining = bundle.expires_in - elapsed
        return remaining <= self._config.expiry_skew_seconds

    def _refresh_and_persist(
        self,
        current: OAuthTokenBundle,
        secrets_seen: list[str | SecretValue],
    ) -> OAuthTokenBundle:
        """Refresh expired/expiring token and persist updated bundle into `UserTokenStore`."""
        if self._refresher is None or current.refresh_token is None:
            raise JiraAuthenticationError(
                "Jira access token expired and no TokenRefresher / refresh_token is available.",
                redact=secrets_seen,
            )
        try:
            refreshed = self._refresher(current.refresh_token)
            merged = OAuthTokenBundle(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token or current.refresh_token,
                expires_in=refreshed.expires_in,
                token_type=refreshed.token_type,
                scope=refreshed.scope or current.scope,
            )
            self._token_store.store(self._config.provider_name, merged, self._config.account)
        except Exception as exc:
            raise JiraAuthenticationError(
                f"Failed to refresh Jira OAuth access token: {exc}",
                redact=secrets_seen,
            ) from exc
        self._cached_bundle = merged
        self._acquired_at = self._clock()
        return merged

    def _send_with_bearer(
        self,
        method: str,
        url: str,
        bundle: OAuthTokenBundle,
        json_body: Mapping[str, object] | None,
        extra_headers: Mapping[str, str] | None,
    ) -> JiraHttpResponse:
        """Attach `Authorization` header and invoke underlying `JiraHttpTransport`."""
        headers = {
            **(dict(extra_headers) if extra_headers is not None else {}),
            "Authorization": f"{bundle.token_type} {bundle.access_token.expose()}",
            "Accept": JSON_CONTENT_TYPE,
        }
        return self._transport.request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            timeout_seconds=self._timeout,
        )


def _resolve_refresher(
    provider_name: str,
    token_refresher: TokenRefresher | None,
    oauth_broker: StatelessOAuthBroker | None,
) -> TokenRefresher | None:
    """Resolve explicit `token_refresher` or bind `oauth_broker.refresh_access_token`."""
    if token_refresher is not None:
        return token_refresher
    if oauth_broker is None:
        return None

    def _broker_refresh(refresh_token: SecretValue) -> OAuthTokenBundle:
        """Delegate refresh-token grant to `StatelessOAuthBroker`."""
        return oauth_broker.refresh_access_token(provider_name, refresh_token)

    return _broker_refresh


def _collect_bundle_secrets(
    bundle: OAuthTokenBundle,
    target: list[str | SecretValue],
) -> None:
    """Append access and refresh `SecretValues` into `target` redaction list."""
    target.append(bundle.access_token)
    if bundle.refresh_token is not None:
        target.append(bundle.refresh_token)


def _extract_jira_error_summary(payload: Mapping[str, object]) -> str:
    """Extract concise human diagnostic from Jira REST v3 error JSON payload."""
    messages = payload.get("errorMessages")
    if isinstance(messages, list) and messages:
        return "; ".join(str(item) for item in messages)
    raw_msg = payload.get("message")
    if isinstance(raw_msg, str) and raw_msg.strip():
        return raw_msg.strip()
    errors = payload.get("errors")
    if isinstance(errors, Mapping) and errors:
        return "; ".join(f"{k}: {v}" for k, v in errors.items())
    return "upstream error"


# endregion
