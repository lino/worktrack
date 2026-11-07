"""Behavioural tests for PKCE S256 helpers and StatelessOAuthBroker."""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from worktrack.secrets import (
    EnvSecretProvider,
    OAuthBrokerError,
    OAuthHttpResponse,
    OAuthInvalidClientError,
    OAuthProviderConfig,
    OAuthTokenBundle,
    PkceChallenge,
    SecretResolver,
    SecretValue,
    StatelessOAuthBroker,
    UrllibOAuthHttpTransport,
    compute_pkce_s256_challenge,
    generate_pkce_pair,
)
from worktrack.telemetry import StructuredEventLogger, TelemetryRecorder

# region Constants
RFC7636_SAMPLE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC7636_EXPECTED_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
MIN_RFC7636_VERIFIER_LEN = 43
MAX_RFC7636_VERIFIER_LEN = 128
STALE_CLIENT_SECRET = "stale-jira-client-secret-111"
ROTATED_CLIENT_SECRET = "rotated-jira-client-secret-222"
SAMPLE_AUTH_CODE = "auth-code-xyz-999"
SAMPLE_ACCESS_TOKEN = "atlassian-access-token-secret-abc"
SAMPLE_REFRESH_TOKEN = "atlassian-refresh-token-secret-def"
SAMPLE_REDIRECT_URI = "http://127.0.0.1:8765/callback"
TOKEN_ENDPOINT_URL = "https://auth.example.test/oauth/token"
HTTP_STATUS_OK = 200
HTTP_STATUS_UNAUTHORIZED = 401
HTTP_STATUS_BAD_REQUEST = 400
SAMPLE_EXPIRES_IN = 3600
EXPECTED_SINGLE_RETRY_ATTEMPTS = 2
TEST_HTTP_TIMEOUT_SECONDS = 5.0
# endregion


# region Fixtures
class RecordingOAuthTransport:
    """Configurable `OAuthHttpTransport` test double recording outbound requests."""

    def __init__(self, expected_client_secret: str) -> None:
        """Initialize transport validating `expected_client_secret`."""
        self.expected_client_secret = expected_client_secret
        self.requests: list[tuple[str, dict[str, str], float]] = []
        self.force_error: str | None = None

    def post_form(
        self,
        url: str,
        form_params: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> OAuthHttpResponse:
        """Validate `client_secret` and return synthetic OAuth token or error JSON."""
        captured = dict(form_params)
        self.requests.append((url, captured, timeout_seconds))
        if self.force_error is not None:
            return OAuthHttpResponse(
                status_code=HTTP_STATUS_BAD_REQUEST,
                payload={
                    "error": self.force_error,
                    "error_description": f"Rejected code={captured.get('code', '')}",
                },
            )
        if captured.get("client_secret") != self.expected_client_secret:
            return OAuthHttpResponse(
                status_code=HTTP_STATUS_UNAUTHORIZED,
                payload={
                    "error": "invalid_client",
                    "error_description": "Client secret mismatch.",
                },
            )
        return OAuthHttpResponse(
            status_code=HTTP_STATUS_OK,
            payload={
                "access_token": SAMPLE_ACCESS_TOKEN,
                "refresh_token": SAMPLE_REFRESH_TOKEN,
                "expires_in": SAMPLE_EXPIRES_IN,
                "token_type": "Bearer",
                "scope": "read:jira-work write:jira-work",
            },
        )


def _make_jira_config() -> OAuthProviderConfig:
    """Create canonical Jira `OAuthProviderConfig` fixture."""
    return OAuthProviderConfig(
        provider="jira",
        client_id="worktrack-jira-client-id",
        client_secret_uri="env://JIRA_CLIENT_SECRET",
        token_endpoint=TOKEN_ENDPOINT_URL,
        scopes=("read:jira-work", "write:jira-work"),
    )


def _assert_zero_secret_leakage(
    *,
    broker: StatelessOAuthBroker,
    event_logger: StructuredEventLogger,
    telemetry: TelemetryRecorder,
    sensitive_values: tuple[str, ...],
) -> str:
    """Assert zero secret plaintext appears in broker state, JSONL events, or metrics."""
    broker_state_dump = repr(vars(broker))
    events_raw = event_logger.events_path.read_text(encoding="utf-8")
    metrics_raw = telemetry.render_metrics_text()
    for sensitive in sensitive_values:
        assert sensitive not in broker_state_dump
        assert sensitive not in events_raw
        assert sensitive not in metrics_raw
    return metrics_raw


# endregion


# region OAuth Broker Tests
def test_pkce_s256_challenge_matches_rfc7636_and_redacts_verifier_repr() -> None:
    """Verify RFC 7636 S256 challenge calculation and verifier redaction."""
    assert compute_pkce_s256_challenge(RFC7636_SAMPLE_VERIFIER) == RFC7636_EXPECTED_CHALLENGE

    challenge = generate_pkce_pair()
    assert isinstance(challenge, PkceChallenge)
    assert MIN_RFC7636_VERIFIER_LEN <= len(challenge.code_verifier) <= MAX_RFC7636_VERIFIER_LEN
    assert challenge.code_challenge_method == "S256"
    assert compute_pkce_s256_challenge(challenge.code_verifier) == challenge.code_challenge
    assert challenge.code_verifier not in repr(challenge)


def test_stateless_broker_exchanges_code_refreshes_and_persists_nothing(tmp_path: Path) -> None:
    """Verify happy-path code exchange, refresh grant, telemetry, and zero persistence."""
    telemetry_dir = tmp_path / "telemetry"
    telemetry = TelemetryRecorder(telemetry_dir=telemetry_dir)
    event_logger = StructuredEventLogger(telemetry_dir=telemetry_dir)
    resolver = SecretResolver(
        providers=(EnvSecretProvider(environ={"JIRA_CLIENT_SECRET": ROTATED_CLIENT_SECRET}),)
    )
    broker = StatelessOAuthBroker(
        secret_resolver=resolver,
        providers=(_make_jira_config(),),
        transport=RecordingOAuthTransport(expected_client_secret=ROTATED_CLIENT_SECRET),
        telemetry=telemetry,
        event_logger=event_logger,
    )
    pkce = generate_pkce_pair()
    bundle = broker.exchange_authorization_code(
        "jira", SAMPLE_AUTH_CODE, pkce.code_verifier, SAMPLE_REDIRECT_URI
    )
    assert isinstance(bundle, OAuthTokenBundle)
    assert isinstance(bundle.access_token, SecretValue)
    assert bundle.access_token.expose() == SAMPLE_ACCESS_TOKEN
    assert (
        bundle.refresh_token is not None and bundle.refresh_token.expose() == SAMPLE_REFRESH_TOKEN
    )
    assert (bundle.expires_in, bundle.token_type) == (SAMPLE_EXPIRES_IN, "Bearer")
    assert SAMPLE_ACCESS_TOKEN not in repr(bundle) and SAMPLE_REFRESH_TOKEN not in repr(bundle)

    refreshed = broker.refresh_access_token("jira", bundle.refresh_token)
    assert refreshed.access_token.expose() == SAMPLE_ACCESS_TOKEN

    metrics_raw = _assert_zero_secret_leakage(
        broker=broker,
        event_logger=event_logger,
        telemetry=telemetry,
        sensitive_values=(
            ROTATED_CLIENT_SECRET,
            SAMPLE_ACCESS_TOKEN,
            SAMPLE_REFRESH_TOKEN,
            SAMPLE_AUTH_CODE,
            pkce.code_verifier,
        ),
    )
    assert 'grant="authorization_code",outcome="ok",provider="jira"} 1.0' in metrics_raw
    assert 'grant="refresh_token",outcome="ok",provider="jira"} 1.0' in metrics_raw


def test_stateless_broker_invalidates_and_retries_once_on_invalid_client(tmp_path: Path) -> None:
    """Verify `invalid_client` triggers cache eviction, re-resolve, and single retry."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    env_store = {"JIRA_CLIENT_SECRET": STALE_CLIENT_SECRET}
    resolver = SecretResolver(providers=(EnvSecretProvider(environ=env_store),))
    assert resolver.resolve("env://JIRA_CLIENT_SECRET").expose() == STALE_CLIENT_SECRET

    env_store["JIRA_CLIENT_SECRET"] = ROTATED_CLIENT_SECRET
    transport = RecordingOAuthTransport(expected_client_secret=ROTATED_CLIENT_SECRET)
    broker = StatelessOAuthBroker(
        secret_resolver=resolver,
        providers=(_make_jira_config(),),
        transport=transport,
        telemetry=telemetry,
    )

    pkce = generate_pkce_pair()
    bundle = broker.exchange_authorization_code(
        provider="jira",
        code=SAMPLE_AUTH_CODE,
        code_verifier=pkce.code_verifier,
        redirect_uri=SAMPLE_REDIRECT_URI,
    )
    assert bundle.access_token.expose() == SAMPLE_ACCESS_TOKEN
    assert len(transport.requests) == EXPECTED_SINGLE_RETRY_ATTEMPTS
    assert transport.requests[0][1]["client_secret"] == STALE_CLIENT_SECRET
    assert transport.requests[1][1]["client_secret"] == ROTATED_CLIENT_SECRET

    metrics_raw = telemetry.render_metrics_text()
    assert (
        'worktrack_oauth_broker_requests_total{grant="authorization_code",'
        'outcome="rotated_ok",provider="jira"} 1.0'
    ) in metrics_raw


def test_stateless_broker_fails_cleanly_after_exhausted_rotation_or_invalid_grant(
    tmp_path: Path,
) -> None:
    """Verify persistent `invalid_client` retries at most once and scrubs errors."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    env_store = {"JIRA_CLIENT_SECRET": STALE_CLIENT_SECRET}
    resolver = SecretResolver(providers=(EnvSecretProvider(environ=env_store),))
    transport = RecordingOAuthTransport(expected_client_secret=ROTATED_CLIENT_SECRET)
    broker = StatelessOAuthBroker(
        secret_resolver=resolver,
        providers=(_make_jira_config(),),
        transport=transport,
        telemetry=telemetry,
    )

    pkce = generate_pkce_pair()
    with pytest.raises(OAuthInvalidClientError) as invalid_client_exc:
        broker.exchange_authorization_code(
            provider="jira",
            code=SAMPLE_AUTH_CODE,
            code_verifier=pkce.code_verifier,
            redirect_uri=SAMPLE_REDIRECT_URI,
        )
    assert len(transport.requests) == EXPECTED_SINGLE_RETRY_ATTEMPTS
    assert STALE_CLIENT_SECRET not in str(invalid_client_exc.value)
    assert SAMPLE_AUTH_CODE not in str(invalid_client_exc.value)

    transport.requests.clear()
    transport.force_error = "invalid_grant"
    with pytest.raises(OAuthBrokerError) as grant_exc:
        broker.exchange_authorization_code(
            provider="jira",
            code=SAMPLE_AUTH_CODE,
            code_verifier=pkce.code_verifier,
            redirect_uri=SAMPLE_REDIRECT_URI,
        )
    assert len(transport.requests) == 1
    assert SAMPLE_AUTH_CODE not in str(grant_exc.value)
    assert STALE_CLIENT_SECRET not in str(grant_exc.value)


def test_urllib_oauth_http_transport_posts_form_and_handles_http_errors() -> None:
    """Verify default `UrllibOAuthHttpTransport` against a local loopback server."""

    class _TokenHandler(BaseHTTPRequestHandler):
        """Loopback OAuth token handler returning 200 or 401 JSON."""

        def do_POST(self) -> None:
            """Parse form body and respond with token or `invalid_client` JSON."""
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            parsed = dict(urllib.parse.parse_qsl(raw_body))
            if parsed.get("client_secret") == ROTATED_CLIENT_SECRET:
                status, body = HTTP_STATUS_OK, {"access_token": SAMPLE_ACCESS_TOKEN}
            else:
                status, body = HTTP_STATUS_UNAUTHORIZED, {"error": "invalid_client"}
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            """Silence default stderr access logs during tests."""
            return

    server = HTTPServer(("127.0.0.1", 0), _TokenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        endpoint = f"http://{host!s}:{port}/oauth/token"
        transport = UrllibOAuthHttpTransport()
        ok_resp = transport.post_form(
            endpoint,
            {"grant_type": "refresh_token", "client_secret": ROTATED_CLIENT_SECRET},
            timeout_seconds=TEST_HTTP_TIMEOUT_SECONDS,
        )
        assert ok_resp.status_code == HTTP_STATUS_OK
        assert ok_resp.payload["access_token"] == SAMPLE_ACCESS_TOKEN

        err_resp = transport.post_form(
            endpoint,
            {"grant_type": "refresh_token", "client_secret": STALE_CLIENT_SECRET},
            timeout_seconds=TEST_HTTP_TIMEOUT_SECONDS,
        )
        assert err_resp.status_code == HTTP_STATUS_UNAUTHORIZED
        assert err_resp.payload["error"] == "invalid_client"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=TEST_HTTP_TIMEOUT_SECONDS)


# endregion
