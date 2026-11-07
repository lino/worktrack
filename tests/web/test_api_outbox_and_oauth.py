"""Behavioural tests for Outbox/EgressLedger API and zero-persistence Stateless OAuth Broker."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from worktrack.config import WorktrackSettings
from worktrack.outbox import (
    ApprovedOutboxGrant,
    OutboxItem,
    SinkWriteReceipt,
    enforce_outbox_grant,
)
from worktrack.secrets import (
    EnvSecretProvider,
    OAuthHttpResponse,
    OAuthProviderConfig,
    SecretResolver,
    StatelessOAuthBroker,
    UrllibOAuthHttpTransport,
    generate_pkce_pair,
)
from worktrack.web import (
    WebServiceContainer,
    ensure_django_configured,
    get_active_web_services,
    reset_default_web_services,
    use_web_services,
)

# region Constants
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_BAD_GATEWAY = 502
SAMPLE_TRACE_ID = "99aabbccddeeff001122334455667788"
SAMPLE_CLIENT_SECRET = "confidential-jira-client-secret-topsecret-999"
SAMPLE_AUTH_CODE = "atlassian-auth-code-ephemeral-777"
SAMPLE_ACCESS_TOKEN = "atlassian-user-access-token-secret-111"
SAMPLE_REFRESH_TOKEN = "atlassian-user-refresh-token-secret-222"
SAMPLE_REDIRECT_URI = "http://127.0.0.1:5173/oauth/callback"
TOKEN_ENDPOINT = "https://auth.atlassian.com/oauth/token"
TOKEN_EXPIRES_IN = 3_600
# endregion


# region Fixtures
class _RecordingSink:
    """In-memory `RollupSink` double verifying `ApprovedOutboxGrant` before recording receipts."""

    def __init__(self) -> None:
        """Initialize empty receipt counter."""
        self.written_ids: list[str] = []

    @property
    def sink_name(self) -> str:
        """Return canonical sink slug (`'jira'`)."""
        return "jira"

    def write_rollup(
        self,
        item: OutboxItem,
        grant: ApprovedOutboxGrant | None = None,
        *,
        source: str = "direct_call",
    ) -> SinkWriteReceipt:
        """Verify sealed grant and return synthetic 201 worklog receipt."""
        enforce_outbox_grant(sink_name=self.sink_name, item=item, grant=grant, source=source)
        self.written_ids.append(item.id)
        return SinkWriteReceipt(
            destination_uri=f"https://api.atlassian.com/issue/{item.target_ref}/worklog",
            http_status=HTTP_CREATED,
            external_receipt_id=f"wl-{len(self.written_ids)}",
        )


class _FakeOAuthTransport:
    """Deterministic `OAuthHttpTransport` test double for broker API tests."""

    def __init__(self, expected_secret: str) -> None:
        """Initialize transport expecting `expected_secret`."""
        self._expected_secret = expected_secret
        self.force_error: str | None = None

    def post_form(
        self,
        url: str,
        form_params: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> OAuthHttpResponse:
        """Return synthetic OAuth token bundle or error response."""
        _ = (url, timeout_seconds)
        if self.force_error is not None:
            return OAuthHttpResponse(
                status_code=HTTP_BAD_REQUEST,
                payload={"error": self.force_error, "error_description": "Invalid grant"},
            )
        if form_params.get("client_secret") != self._expected_secret:
            return OAuthHttpResponse(status_code=401, payload={"error": "invalid_client"})
        return OAuthHttpResponse(
            status_code=HTTP_OK,
            payload={
                "access_token": SAMPLE_ACCESS_TOKEN,
                "refresh_token": SAMPLE_REFRESH_TOKEN,
                "expires_in": TOKEN_EXPIRES_IN,
                "token_type": "Bearer",
                "scope": "read:jira-work write:jira-work",
            },
        )


def _seed_outbox_intervals(client: Any) -> None:
    """Seed two closed Targeted `Intervals` with private local notes via API."""
    for name, ref, hour in (("Ticket A", "PROJ-10", "08"), ("Ticket B", "PROJ-11", "09")):
        act_id = client.post(
            "/api/v1/activities/",
            {"name": name, "kind": "targeted", "external_ref": ref},
            format="json",
        ).json()["id"]
        client.post(
            "/api/v1/intervals/",
            {
                "capture_mode": "structured",
                "activity_id": act_id,
                "started_at": f"2026-11-07T{hour}:00:00Z",
                "ended_at": f"2026-11-07T{hour}:30:00Z",
                "notes": "Private local note never exported",
            },
            format="json",
        )


def _build_oauth_container(tmp_path: Path) -> tuple[WebServiceContainer, _FakeOAuthTransport]:
    """Construct `WebServiceContainer` wired with a test `StatelessOAuthBroker`."""
    settings = WorktrackSettings(
        config_file=tmp_path / "cfg" / "config.toml",
        db_path=tmp_path / "data" / "ledger.sqlite3",
        socket_path=tmp_path / "run" / "wt.sock",
        telemetry_dir=tmp_path / "telemetry",
    )
    resolver = SecretResolver(
        providers=(EnvSecretProvider(environ={"JIRA_SECRET": SAMPLE_CLIENT_SECRET}),)
    )
    transport = _FakeOAuthTransport(expected_secret=SAMPLE_CLIENT_SECRET)
    base = WebServiceContainer.from_settings(settings)
    broker = StatelessOAuthBroker(
        secret_resolver=resolver,
        providers=(
            OAuthProviderConfig(
                provider="jira",
                client_id="wt-jira-client",
                client_secret_uri="env://JIRA_SECRET",
                token_endpoint=TOKEN_ENDPOINT,
            ),
        ),
        transport=transport,
        telemetry=base.telemetry,
        event_logger=base.event_logger,
    )
    return base.with_oauth_broker(broker), transport


def _assert_zero_secret_persistence(
    container: WebServiceContainer,
    secrets_list: Sequence[str],
) -> None:
    """Assert none of `secrets_list` appear in SQLite, JSONL events, or Prometheus metrics."""
    sqlite_dump = ""
    if container.settings.db_path.exists():
        with closing(sqlite3.connect(container.settings.db_path)) as conn:
            sqlite_dump = "\n".join(conn.iterdump())
    events_raw = container.event_logger.events_path.read_text(encoding="utf-8")
    metrics_raw = container.telemetry.render_metrics_text()
    for secret_str in secrets_list:
        assert secret_str not in sqlite_dump
        assert secret_str not in events_raw
        assert secret_str not in metrics_raw


# endregion


# region Outbox & OAuth Broker API Tests
def test_outbox_preview_approve_reject_flush_and_egress_ledger_api(tmp_path: Path) -> None:
    """Verify Outbox preview, staging, approval/rejection, flush, and read-only EgressLedger."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    settings = WorktrackSettings(
        config_file=tmp_path / "cfg" / "config.toml",
        db_path=tmp_path / "data" / "ledger.sqlite3",
        socket_path=tmp_path / "run" / "wt.sock",
        telemetry_dir=tmp_path / "telemetry",
    )
    container = WebServiceContainer.from_settings(settings, sinks=(_RecordingSink(),))

    with use_web_services(container):
        client = APIClient()
        _seed_outbox_intervals(client)

        dry = client.post("/api/v1/outbox/preview/", {"sink": "jira"}, format="json").json()
        assert len(dry["diffs"]) == 2 and dry["staged_items"] == []

        staged = client.post(
            "/api/v1/outbox/preview/",
            {"sink": "jira", "stage": True, "flush_cadence": "manual"},
            format="json",
        ).json()["staged_items"]
        assert len(staged) == 2 and "Private local note" not in str(staged[0]["payload_preview"])

        first_id, second_id = staged[0]["id"], staged[1]["id"]
        assert client.post(f"/api/v1/outbox/items/{first_id}/approve/").status_code == HTTP_OK
        rej = client.post(
            f"/api/v1/outbox/items/{second_id}/reject/",
            {"reason": "Hold until Monday"},
            format="json",
        )
        assert rej.status_code == HTTP_OK and rej.json()["rejection_reason"] == "Hold until Monday"

        flush_resp = client.post(
            "/api/v1/outbox/flush/",
            {"sink": "jira", "trigger": "manual"},
            format="json",
            HTTP_X_FRITZ_TRACE_ID=SAMPLE_TRACE_ID,
        )
        results = flush_resp.json()["results"]
        assert len(results) == 1 and results[0]["egress_record"]["trace_id"] == SAMPLE_TRACE_ID

        records = client.get("/api/v1/egress-ledger/?sink=jira&outcome=ok").json()
        assert len(records) == 1 and records[0]["external_receipt_id"] == "wl-1"
        assert client.post("/api/v1/egress-ledger/", {}, "json").status_code == (
            HTTP_METHOD_NOT_ALLOWED
        )


def test_stateless_oauth_broker_endpoints_persist_zero_tokens_or_secrets(tmp_path: Path) -> None:
    """Verify `/api/v1/oauth/{provider}/*/` set no-store and persist zero secrets."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    container, _ = _build_oauth_container(tmp_path)
    pkce = generate_pkce_pair()

    with use_web_services(container):
        client = APIClient()
        ex_resp = client.post(
            "/api/v1/oauth/jira/exchange/",
            {
                "code": SAMPLE_AUTH_CODE,
                "code_verifier": pkce.code_verifier,
                "redirect_uri": SAMPLE_REDIRECT_URI,
            },
            format="json",
        )
        assert ex_resp.status_code == HTTP_OK and ex_resp["Cache-Control"] == "no-store"
        assert ex_resp.json()["access_token"] == SAMPLE_ACCESS_TOKEN

        ref_resp = client.post(
            "/api/v1/oauth/jira/refresh/",
            {"refresh_token": SAMPLE_REFRESH_TOKEN},
            format="json",
        )
        assert ref_resp.status_code == HTTP_OK and ref_resp["Cache-Control"] == "no-store"

    _assert_zero_secret_persistence(
        container,
        (
            SAMPLE_CLIENT_SECRET,
            SAMPLE_AUTH_CODE,
            SAMPLE_ACCESS_TOKEN,
            SAMPLE_REFRESH_TOKEN,
            pkce.code_verifier,
        ),
    )


def test_stateless_oauth_broker_scrubs_errors_and_sets_no_store(tmp_path: Path) -> None:
    """Verify upstream OAuth failures return scrubbed errors with `Cache-Control: no-store`."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    container, transport = _build_oauth_container(tmp_path)
    transport.force_error = "invalid_grant"
    pkce = generate_pkce_pair()

    with use_web_services(container):
        client = APIClient()
        err_resp = client.post(
            "/api/v1/oauth/jira/exchange/",
            {
                "code": SAMPLE_AUTH_CODE,
                "code_verifier": pkce.code_verifier,
                "redirect_uri": SAMPLE_REDIRECT_URI,
            },
            format="json",
        )
        assert err_resp.status_code in (HTTP_BAD_REQUEST, HTTP_BAD_GATEWAY)
        assert err_resp["Cache-Control"] == "no-store"
        err_text = err_resp.content.decode("utf-8")
        assert SAMPLE_CLIENT_SECRET not in err_text and SAMPLE_AUTH_CODE not in err_text

    _assert_zero_secret_persistence(container, (SAMPLE_CLIENT_SECRET, SAMPLE_AUTH_CODE))


def test_default_web_services_caches_secret_resolver_across_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sequential requests outside `use_web_services` share `SecretResolver` TTL cache."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    secret_file = tmp_path / "jira_secret"
    secret_file.write_text(SAMPLE_CLIENT_SECRET, encoding="utf-8")
    secret_file.chmod(0o600)
    for k, v in {
        "WORKTRACK_DB_PATH": str(tmp_path / "data" / "ledger.sqlite3"),
        "WORKTRACK_TELEMETRY_DIR": str(tmp_path / "telemetry"),
        "WORKTRACK_OAUTH_JIRA_CLIENT_ID": "wt-jira-client",
        "WORKTRACK_OAUTH_JIRA_CLIENT_SECRET_URI": f"file://{secret_file}",
        "WORKTRACK_OAUTH_JIRA_TOKEN_ENDPOINT": TOKEN_ENDPOINT,
    }.items():
        monkeypatch.setenv(k, v)
    reset_default_web_services()

    transport = _FakeOAuthTransport(expected_secret=SAMPLE_CLIENT_SECRET)
    monkeypatch.setattr(
        UrllibOAuthHttpTransport,
        "post_form",
        lambda _self, url, form_params, *, timeout_seconds: transport.post_form(
            url, form_params, timeout_seconds=timeout_seconds
        ),
    )
    pkce = generate_pkce_pair()
    body = {
        "code": SAMPLE_AUTH_CODE,
        "code_verifier": pkce.code_verifier,
        "redirect_uri": SAMPLE_REDIRECT_URI,
    }
    client = APIClient()
    assert client.post("/api/v1/oauth/jira/exchange/", body, format="json").status_code == HTTP_OK
    secret_file.write_text("rotated-jira-secret-v2", encoding="utf-8")
    assert client.post("/api/v1/oauth/jira/exchange/", body, format="json").status_code == HTTP_OK
    transport._expected_secret = "rotated-jira-secret-v2"
    assert client.post("/api/v1/oauth/jira/exchange/", body, format="json").status_code == HTTP_OK

    metrics = get_active_web_services().telemetry.render_metrics_text()
    assert (
        'worktrack_secret_provider_resolves_total{outcome="cache_hit",scheme="file"} 2.0' in metrics
    )
    assert (
        'worktrack_oauth_broker_requests_total{grant="authorization_code",'
        'outcome="rotated_ok",provider="jira"} 1.0' in metrics
    )


def test_web_service_container_wires_jira_sink_in_node_mode_only(tmp_path: Path) -> None:
    """Verify `WebServiceContainer` wires `JiraWorklogSink` in `node` mode and `()` in `hub`."""
    jira_env = {
        "WORKTRACK_DB_PATH": str(tmp_path / "data" / "ledger.sqlite3"),
        "WORKTRACK_TELEMETRY_DIR": str(tmp_path / "telemetry"),
        "WORKTRACK_TOKEN_STORE_PATH": str(tmp_path / "cfg" / "tokens.json"),
        "WORKTRACK_JIRA_CLOUD_ID": "cloud-999",
    }
    node_container = WebServiceContainer.from_env(environ={**jira_env, "WORKTRACK_MODE": "node"})
    assert [s.sink_name for s in node_container.sinks] == ["jira"]

    unconfigured = WebServiceContainer.from_env(
        environ={k: v for k, v in jira_env.items() if k != "WORKTRACK_JIRA_CLOUD_ID"}
    )
    assert unconfigured.sinks == ()

    hub_container = WebServiceContainer.from_env(environ={**jira_env, "WORKTRACK_MODE": "hub"})
    assert hub_container.sinks == ()


# endregion
