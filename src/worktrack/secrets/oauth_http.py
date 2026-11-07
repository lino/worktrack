"""PKCE S256 helpers, OAuth models, and HTTP transport for `worktrack.secrets`."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from worktrack.secrets.exceptions import OAuthBrokerError, SecretValue
from worktrack.secrets.uri import SecretUri

# region Constants
PKCE_CODE_CHALLENGE_METHOD_S256 = "S256"
DEFAULT_PKCE_VERIFIER_BYTES = 32
MIN_PKCE_VERIFIER_LENGTH = 43
MAX_PKCE_VERIFIER_LENGTH = 128
PKCE_VERIFIER_PATTERN = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
PROVIDER_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

DEFAULT_TOKEN_TYPE = "Bearer"
HTTPS_SCHEME = "https"
HTTP_SCHEME = "http"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
JSON_ACCEPT_TYPE = "application/json"
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class PkceChallenge:
    """RFC 7636 S256 PKCE verifier and challenge pair (`code_verifier` redacted in repr)."""

    code_verifier: str = field(repr=False)
    code_challenge: str
    code_challenge_method: str = PKCE_CODE_CHALLENGE_METHOD_S256


@dataclass(frozen=True, slots=True)
class OAuthProviderConfig:
    """Declarative OAuth 2.0 confidential-client configuration for an external provider.

    Attributes:
        provider: Bounded lowercase provider slug (`jira`, `github`, etc.).
        client_id: Public OAuth client identifier.
        client_secret_uri: `SecretUri` string resolving the confidential `client_secret`.
        token_endpoint: Upstream OAuth 2.0 token endpoint URL (`https://...`).
        scopes: Tuple of requested OAuth scope strings.
    """

    provider: str
    client_id: str
    client_secret_uri: str
    token_endpoint: str
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate provider slug, client_id, `SecretUri`, and endpoint URL."""
        if not PROVIDER_SLUG_PATTERN.match(self.provider):
            raise OAuthBrokerError(
                f"Invalid OAuth provider slug '{self.provider}'; use lowercase [a-z0-9_-]."
            )
        if not self.client_id.strip():
            raise OAuthBrokerError("OAuth client_id must be non-empty.")
        SecretUri.parse(self.client_secret_uri)
        validate_token_endpoint_url(self.token_endpoint)


@dataclass(frozen=True, slots=True)
class OAuthTokenBundle:
    """Ephemeral OAuth token response returned directly to the calling client.

    Attributes:
        access_token: Redacted `SecretValue` holding the bearer access token.
        refresh_token: Optional redacted `SecretValue` holding the refresh token.
        expires_in: Optional access-token lifetime in seconds.
        token_type: OAuth token type (defaults to `Bearer`).
        scope: Optional space-delimited granted OAuth scope string.
    """

    access_token: SecretValue
    refresh_token: SecretValue | None = None
    expires_in: int | None = None
    token_type: str = DEFAULT_TOKEN_TYPE
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthHttpResponse:
    """Decoded HTTP status code and JSON body from an OAuth token endpoint."""

    status_code: int
    payload: Mapping[str, object]


# endregion


# region PKCE
def compute_pkce_s256_challenge(code_verifier: str) -> str:
    """Compute RFC 7636 S256 `code_challenge` for a validated `code_verifier`."""
    if not PKCE_VERIFIER_PATTERN.match(code_verifier):
        raise OAuthBrokerError(
            f"PKCE code_verifier must be {MIN_PKCE_VERIFIER_LENGTH}..{MAX_PKCE_VERIFIER_LENGTH} "
            "RFC 7636 unreserved characters."
        )
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_pkce_pair(verifier_bytes: int = DEFAULT_PKCE_VERIFIER_BYTES) -> PkceChallenge:
    """Generate a fresh RFC 7636 S256 `PkceChallenge` (`code_verifier` + `code_challenge`)."""
    raw_verifier = base64.urlsafe_b64encode(secrets.token_bytes(verifier_bytes)).rstrip(b"=")
    verifier = raw_verifier.decode("ascii")
    if not (MIN_PKCE_VERIFIER_LENGTH <= len(verifier) <= MAX_PKCE_VERIFIER_LENGTH):
        raise OAuthBrokerError(
            f"verifier_bytes={verifier_bytes} produced verifier length {len(verifier)} "
            f"outside {MIN_PKCE_VERIFIER_LENGTH}..{MAX_PKCE_VERIFIER_LENGTH}."
        )
    return PkceChallenge(
        code_verifier=verifier,
        code_challenge=compute_pkce_s256_challenge(verifier),
    )


# endregion


# region HTTP Transport
class OAuthHttpTransport(Protocol):
    """Injectable HTTP transport protocol for OAuth 2.0 token-endpoint requests."""

    def post_form(
        self,
        url: str,
        form_params: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> OAuthHttpResponse:
        """POST URL-encoded `form_params` to `url` and decode JSON payload."""
        ...


class UrllibOAuthHttpTransport:
    """Default `urllib.request` OAuth token-endpoint form-POST transport."""

    def post_form(
        self,
        url: str,
        form_params: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> OAuthHttpResponse:
        """Execute form-encoded POST and parse 2xx or 4xx/5xx JSON body safely."""
        validate_token_endpoint_url(url)
        encoded_body = urllib.parse.urlencode(form_params).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=encoded_body,
            headers={
                "Content-Type": FORM_CONTENT_TYPE,
                "Accept": JSON_ACCEPT_TYPE,
            },
            method="POST",
        )
        secret_vals = tuple(form_params.values())
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as resp:
                raw_bytes = resp.read()
                return OAuthHttpResponse(
                    status_code=int(resp.status),
                    payload=_decode_json_object(raw_bytes, secret_vals),
                )
        except urllib.error.HTTPError as exc:
            err_bytes = exc.read()
            payload = _decode_json_object(err_bytes, secret_vals, allow_non_json=True)
            return OAuthHttpResponse(status_code=int(exc.code), payload=payload)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OAuthBrokerError(
                "Upstream OAuth token endpoint request failed.",
                redact=secret_vals,
            ) from exc


def validate_token_endpoint_url(url: str) -> None:
    """Verify OAuth token endpoint uses HTTPS (or loopback HTTP for local testing)."""
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme == HTTPS_SCHEME and host:
        return
    if parsed.scheme == HTTP_SCHEME and host in LOOPBACK_HOSTS:
        return
    raise OAuthBrokerError(
        "OAuth token_endpoint must use 'https://' (or loopback 'http://127.0.0.1' in tests)."
    )


def _decode_json_object(
    raw_bytes: bytes,
    secret_vals: tuple[str, ...],
    *,
    allow_non_json: bool = False,
) -> Mapping[str, object]:
    """Decode UTF-8 JSON object from upstream OAuth HTTP response bytes."""
    text = raw_bytes.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        if allow_non_json:
            return {"error": "http_error"}
        raise OAuthBrokerError(
            "Upstream OAuth token endpoint returned invalid JSON.",
            redact=(*secret_vals, text),
        ) from exc
    if not isinstance(parsed, Mapping):
        if allow_non_json:
            return {"error": "http_error"}
        raise OAuthBrokerError(
            "Upstream OAuth token endpoint JSON must be an object.",
            redact=secret_vals,
        )
    return parsed


# endregion
