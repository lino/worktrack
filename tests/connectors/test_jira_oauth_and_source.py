"""Behavioural tests for `JiraOAuthClient` and `JiraIssueSource` (`worktrack.connectors.jira`)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from worktrack.connectors.jira import (
    DEFAULT_JIRA_JQL,
    JIRA_PROVIDER_SLUG,
    JIRA_SOURCE_SLUG,
    JiraApiError,
    JiraAuthenticationError,
    JiraCloudConfig,
    JiraHttpResponse,
    JiraIssueSource,
    JiraOAuthClient,
    UrllibJiraHttpTransport,
)
from worktrack.core import (
    ACTIVITY_KIND_TARGETED,
    ENTITY_KIND_COMPONENT,
    ENTITY_KIND_EPIC,
    ENTITY_KIND_PROJECT,
    Ledger,
)
from worktrack.secrets import (
    REDACTED_PLACEHOLDER,
    EnvSecretProvider,
    OAuthHttpResponse,
    OAuthProviderConfig,
    OAuthTokenBundle,
    SecretResolver,
    SecretValue,
    StatelessOAuthBroker,
    UserTokenStore,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_INTERNAL_ERROR = 500
NEAR_EXPIRY_SECONDS = 15
FRESH_EXPIRY_SECONDS = 3_600
TEST_TIMEOUT_SECONDS = 5.0
WEIGHT_HALF = 0.5
WEIGHT_FULL = 1.0
STALE_ACCESS = "atok_stale_secret_value_111"
INITIAL_REFRESH = "rtok_initial_secret_value_222"
FRESH_ACCESS = "atok_fresh_secret_value_333"
ROTATED_REFRESH = "rtok_rotated_secret_value_444"
# endregion


# region Fixtures
class _InMemoryKeyring:
    """In-memory `KeyringBackend` double for workstation `UserTokenStore` tests."""

    def __init__(self) -> None:
        """Initialize empty password map."""
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        """Fetch stored credential JSON."""
        return self._data.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """Persist credential JSON."""
        self._data[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        """Delete credential JSON."""
        self._data.pop((service_name, username), None)


class _RecordingJiraTransport:
    """Injectable `JiraHttpTransport` test double recording outbound requests."""

    def __init__(self, responses: list[JiraHttpResponse]) -> None:
        """Initialize transport with queued `JiraHttpResponse` items."""
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float = TEST_TIMEOUT_SECONDS,
    ) -> JiraHttpResponse:
        """Record request details and pop next queued response."""
        _ = timeout_seconds
        self.calls.append(
            (method, url, dict(headers), None if json_body is None else dict(json_body))
        )
        return self._responses.pop(0)


class _FakeOAuthBrokerTransport:
    """Injectable `OAuthHttpTransport` returning a refreshed token bundle."""

    def __init__(self, access_token: str, refresh_token: str) -> None:
        """Initialize fake upstream OAuth token response."""
        self._access = access_token
        self._refresh = refresh_token
        self.forms: list[dict[str, str]] = []

    def post_form(
        self,
        url: str,
        form_params: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> OAuthHttpResponse:
        """Record form POST and return refreshed token JSON."""
        _ = (url, timeout_seconds)
        self.forms.append(dict(form_params))
        return OAuthHttpResponse(
            status_code=HTTP_OK,
            payload={
                "access_token": self._access,
                "refresh_token": self._refresh,
                "expires_in": FRESH_EXPIRY_SECONDS,
                "token_type": "Bearer",
            },
        )


def _build_stateless_jira_broker(access: str, refresh: str) -> StatelessOAuthBroker:
    """Create a `StatelessOAuthBroker` configured for Jira refresh tests."""
    resolver = SecretResolver(
        providers=[EnvSecretProvider(environ={"JIRA_CLIENT_SECRET": "app_client_secret_999"})]
    )
    return StatelessOAuthBroker(
        secret_resolver=resolver,
        providers=[
            OAuthProviderConfig(
                provider=JIRA_PROVIDER_SLUG,
                client_id="jira-client-id",
                client_secret_uri="env://JIRA_CLIENT_SECRET",
                token_endpoint="https://auth.atlassian.com/oauth/token",
            )
        ],
        transport=_FakeOAuthBrokerTransport(access, refresh),
    )


class _LoopbackJiraHandler(BaseHTTPRequestHandler):
    """Minimal loopback HTTP handler simulating Jira Cloud REST v3."""

    def do_POST(self) -> None:
        """Echo posted JSON JQL and status 200."""
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        encoded = json.dumps({"ok": True, "received_jql": body.get("jql")}).encode("utf-8")
        self.send_response(HTTP_OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        """Silence test HTTP server stderr logging."""
        _ = (format, args)


@contextmanager
def _loopback_jira_server() -> Iterator[str]:
    """Run a background loopback HTTP server and yield its base URL."""
    server = HTTPServer(("127.0.0.1", 0), _LoopbackJiraHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _sample_jira_search_responses() -> list[JiraHttpResponse]:
    """Build two paginated JQL responses followed by an error response."""
    proj_payload = {"id": "200", "key": "PROJ", "name": "Worktrack Platform"}
    issue_one = {
        "id": "10001",
        "key": "PROJ-142",
        "fields": {
            "summary": "Implement Outbox Gate",
            "status": {"statusCategory": {"key": "indeterminate"}},
            "project": proj_payload,
            "components": [
                {"id": "301", "name": "Outbox Core"},
                {"id": "302", "name": "Jira Connector"},
            ],
            "parent": {
                "id": "10000",
                "key": "PROJ-10",
                "fields": {"summary": "Anti-Surveillance Rollups", "issuetype": {"name": "Epic"}},
            },
            "labels": ["wave-1", "security"],
        },
    }
    issue_two = {
        "id": "10002",
        "key": "PROJ-143",
        "fields": {
            "summary": "Write Zensical Runbook",
            "status": {"statusCategory": {"key": "new"}},
            "project": proj_payload,
            "components": [],
            "labels": [],
        },
    }
    return [
        JiraHttpResponse(HTTP_OK, {"issues": [issue_one], "nextPageToken": "tok-page-2"}),
        JiraHttpResponse(HTTP_OK, {"issues": [issue_two], "isLast": True}),
        JiraHttpResponse(HTTP_INTERNAL_ERROR, {"errorMessages": ["Outage"]}),
    ]


# endregion


# region Jira OAuth Tests
def test_jira_oauth_client_refreshes_401_via_stateless_broker_and_persists_locally(
    tmp_path: Path,
) -> None:
    """Verify `401` triggers `StatelessOAuthBroker` refresh and updates `UserTokenStore`."""
    token_store = UserTokenStore(
        keyring_backend=_InMemoryKeyring(),
        fallback_path=tmp_path / "unused_tokens.json",
        allow_file_fallback=False,
    )
    token_store.store(
        JIRA_PROVIDER_SLUG,
        OAuthTokenBundle(
            access_token=SecretValue(STALE_ACCESS),
            refresh_token=SecretValue(INITIAL_REFRESH),
            expires_in=FRESH_EXPIRY_SECONDS,
        ),
    )
    jira_transport = _RecordingJiraTransport(
        [
            JiraHttpResponse(status_code=HTTP_UNAUTHORIZED, payload={"message": "Token expired"}),
            JiraHttpResponse(status_code=HTTP_OK, payload={"accountId": "acct-1"}),
        ]
    )
    client = JiraOAuthClient(
        config=JiraCloudConfig(cloud_id="cloud-abc-123"),
        token_store=token_store,
        oauth_broker=_build_stateless_jira_broker(FRESH_ACCESS, ROTATED_REFRESH),
        transport=jira_transport,
    )

    resp = client.request("GET", "/rest/api/3/myself")
    assert resp.status_code == HTTP_OK
    assert len(jira_transport.calls) == 2
    assert jira_transport.calls[0][2]["Authorization"] == f"Bearer {STALE_ACCESS}"
    assert jira_transport.calls[1][2]["Authorization"] == f"Bearer {FRESH_ACCESS}"

    persisted = token_store.load(JIRA_PROVIDER_SLUG)
    assert persisted is not None and persisted.refresh_token is not None
    assert persisted.access_token.expose() == FRESH_ACCESS
    assert persisted.refresh_token.expose() == ROTATED_REFRESH
    assert not (tmp_path / "unused_tokens.json").exists()


def test_jira_oauth_client_proactive_near_expiry_refresh_and_secret_scrubbing(
    tmp_path: Path,
) -> None:
    """Verify near-expiry tokens refresh proactively and error messages scrub tokens."""
    token_store = UserTokenStore(
        keyring_backend=_InMemoryKeyring(), fallback_path=tmp_path / "tokens.json"
    )
    refresh_secret = "rtok_proactive_secret"
    renewed_access = "atok_renewed_proactive_secret"
    token_store.store(
        JIRA_PROVIDER_SLUG,
        OAuthTokenBundle(
            access_token=SecretValue("atok_expiring_soon_secret"),
            refresh_token=SecretValue(refresh_secret),
            expires_in=NEAR_EXPIRY_SECONDS,
        ),
    )
    refreshed_calls: list[str] = []

    def _refresh_cb(refresh_token: SecretValue) -> OAuthTokenBundle:
        """Refresh callback returning renewed access token without rotating refresh token."""
        refreshed_calls.append(refresh_token.expose())
        return OAuthTokenBundle(
            access_token=SecretValue(renewed_access), expires_in=FRESH_EXPIRY_SECONDS
        )

    jira_transport = _RecordingJiraTransport(
        [
            JiraHttpResponse(
                status_code=HTTP_INTERNAL_ERROR,
                payload={"errorMessages": [f"Echoed Bearer {renewed_access} and {refresh_secret}"]},
            )
        ]
    )
    client = JiraOAuthClient(
        config=JiraCloudConfig(cloud_id="cloud-xyz"),
        token_store=token_store,
        token_refresher=_refresh_cb,
        transport=jira_transport,
    )
    with pytest.raises(JiraApiError) as exc_info:
        client.request("GET", "/rest/api/3/myself")

    assert refreshed_calls == [refresh_secret]
    assert renewed_access not in str(exc_info.value) and refresh_secret not in str(exc_info.value)
    assert REDACTED_PLACEHOLDER in str(exc_info.value)

    persisted = token_store.load(JIRA_PROVIDER_SLUG)
    assert persisted is not None and persisted.refresh_token is not None
    assert persisted.refresh_token.expose() == refresh_secret


def test_urllib_jira_transport_round_trip_and_missing_token_error(tmp_path: Path) -> None:
    """Verify stdlib `UrllibJiraHttpTransport` JSON round-trip and missing-token rejection."""
    token_store = UserTokenStore(
        keyring_backend=_InMemoryKeyring(), fallback_path=tmp_path / "empty_tokens.json"
    )
    with _loopback_jira_server() as base_url:
        client = JiraOAuthClient(
            config=JiraCloudConfig(api_base_url=base_url),
            token_store=token_store,
            transport=UrllibJiraHttpTransport(),
        )
        with pytest.raises(JiraAuthenticationError):
            client.request("POST", "/rest/api/3/search/jql", json_body={"jql": DEFAULT_JIRA_JQL})

        token_store.store(
            JIRA_PROVIDER_SLUG,
            OAuthTokenBundle(
                access_token=SecretValue("loopback_access_token"), expires_in=FRESH_EXPIRY_SECONDS
            ),
        )
        resp = client.request("POST", "/rest/api/3/search/jql", json_body={"jql": DEFAULT_JIRA_JQL})
        assert resp.status_code == HTTP_OK
        assert resp.payload["received_jql"] == DEFAULT_JIRA_JQL


# endregion


# region Jira Source & Sink Tests
def _assert_synced_proj_142_graph(ledger: Ledger) -> None:
    """Assert `PROJ-142` Targeted `Activity`, `Entities`, and normalized `Attributions`."""
    act_142 = ledger.find_activity_by_external_ref("PROJ-142", source=JIRA_SOURCE_SLUG)
    assert act_142 is not None and act_142.kind == ACTIVITY_KIND_TARGETED
    assert (act_142.name, act_142.tags) == ("Implement Outbox Gate", ("security", "wave-1"))

    proj = ledger.find_entity(ENTITY_KIND_PROJECT, "PROJ")
    epic = ledger.find_entity(ENTITY_KIND_EPIC, "PROJ-10")
    comp_outbox = ledger.find_entity(ENTITY_KIND_COMPONENT, "outbox-core")
    comp_jira = ledger.find_entity(ENTITY_KIND_COMPONENT, "jira-connector")
    assert (
        proj is not None and epic is not None and comp_outbox is not None and comp_jira is not None
    )

    weights = ledger.validate_outgoing_weights(source_activity_id=act_142.id)
    for kind in (ENTITY_KIND_PROJECT, ENTITY_KIND_EPIC, ENTITY_KIND_COMPONENT):
        assert weights[kind] == pytest.approx(WEIGHT_FULL)
    edges = {
        e.target_entity_id: e.weight
        for e in ledger.list_attributions(source_activity_id=act_142.id)
    }
    assert edges[comp_outbox.id] == pytest.approx(WEIGHT_HALF)
    assert edges[comp_jira.id] == pytest.approx(WEIGHT_HALF)


def test_jira_issue_source_syncs_activities_entities_and_attributions(tmp_path: Path) -> None:
    """Verify `JiraIssueSource` upserts Targeted `Activities`, `Entities`, and `Attributions`."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    token_store = UserTokenStore(
        keyring_backend=_InMemoryKeyring(), fallback_path=tmp_path / "tokens.json"
    )
    token_store.store(
        JIRA_PROVIDER_SLUG,
        OAuthTokenBundle(
            access_token=SecretValue("jira_sync_token"), expires_in=FRESH_EXPIRY_SECONDS
        ),
    )
    transport = _RecordingJiraTransport(_sample_jira_search_responses())
    source = JiraIssueSource(
        client=JiraOAuthClient(
            config=JiraCloudConfig(cloud_id="cloud-sync-1"),
            token_store=token_store,
            transport=transport,
        ),
        telemetry=telemetry,
    )

    with Ledger(tmp_path / "ledger.sqlite3") as ledger:
        result = source.sync(ledger)
        assert (result.issues_fetched, result.activities_created) == (2, 2)
        assert (
            transport.calls[0][3] is not None and transport.calls[0][3]["jql"] == DEFAULT_JIRA_JQL
        )
        _assert_synced_proj_142_graph(ledger)
        with pytest.raises(JiraApiError):
            source.sync(ledger, jql="project = PROJ AND status = Open")

    metrics_text = telemetry.render_metrics_text()
    assert 'worktrack_source_syncs_total{outcome="ok",source="jira"} 1.0' in metrics_text
    assert 'worktrack_source_syncs_total{outcome="error",source="jira"} 1.0' in metrics_text


# endregion
