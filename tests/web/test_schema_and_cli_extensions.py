"""Behavioural tests for deterministic OpenAPI 3.1 export and `wt outbox`/`jira`/`serve` CLI."""

from __future__ import annotations

import io
import json
import threading
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from worktrack.cli import (
    CANONICAL_CLI_SUBCOMMANDS,
    EXIT_SUCCESS,
    LedgerIpcServiceProvider,
    main,
)
from worktrack.connectors import DEFAULT_JIRA_HTTP_TIMEOUT_SECONDS, JiraHttpResponse
from worktrack.outbox import ApprovedOutboxGrant, OutboxItem, SinkWriteReceipt, enforce_outbox_grant
from worktrack.telemetry.catalog import CANONICAL_LABEL_VALUE_CEILING
from worktrack.web.schema_export import (
    DEFAULT_SCHEMA_OUTPUT_PATH,
    export_openapi_schema,
    validate_openapi_schema_zero_warnings,
)
from worktrack.web.schema_export import (
    main as schema_export_main,
)

# region Constants
HTTP_OK = 200
HTTP_CREATED = 201
EXPECTED_COMPONENT_SCHEMAS: frozenset[str] = frozenset(
    {
        "Activity",
        "Interval",
        "Entity",
        "Attribution",
        "Facet",
        "ContextFact",
        "AgendaItem",
        "FocusBlock",
        "OutboxItem",
        "PreviewDiff",
        "EgressRecord",
        "EgressReceipt",
        "NowInsight",
        "TodayInsight",
        "WeekInsight",
    }
)
SAMPLE_JIRA_ACCESS_TOKEN = "jira-local-keyring-access-token-secret"
# endregion


# region Fixtures
def _cli_env(tmp_path: Path) -> dict[str, str]:
    """Create isolated environment dictionary for `wt` CLI tests."""
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "WORKTRACK_DB_PATH": str(tmp_path / "data" / "ledger.sqlite3"),
        "WORKTRACK_SOCKET_PATH": str(tmp_path / "run" / "wt.sock"),
        "WORKTRACK_TELEMETRY_DIR": str(tmp_path / "telemetry"),
        "WORKTRACK_TOKEN_STORE_PATH": str(tmp_path / "cfg" / "oauth_tokens.json"),
    }


def _invoke_wt(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """Invoke `wt` CLI `main` with in-memory streams."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = main(argv, environ=env, stdout=out_buf, stderr=err_buf)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class _FakeJiraTransport:
    """Injectable `JiraHttpTransport` double returning a deterministic JQL search page."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float = DEFAULT_JIRA_HTTP_TIMEOUT_SECONDS,
    ) -> JiraHttpResponse:
        """Return synthetic Jira Cloud issue payload."""
        _ = (method, url, headers, json_body, timeout_seconds)
        return JiraHttpResponse(
            status_code=HTTP_OK,
            payload={
                "issues": [
                    {
                        "key": "PROJ-888",
                        "fields": {
                            "summary": "CLI Synced Jira Issue",
                            "project": {"key": "PROJ", "name": "Worktrack"},
                            "components": [{"name": "API"}],
                            "labels": ["wp07"],
                        },
                    }
                ],
                "isLast": True,
            },
        )


class _CliStubSink:
    """In-memory `RollupSink` for testing `wt outbox flush`."""

    @property
    def sink_name(self) -> str:
        """Return `'jira'` sink slug."""
        return "jira"

    def write_rollup(
        self,
        item: OutboxItem,
        grant: ApprovedOutboxGrant | None = None,
        *,
        source: str = "direct_call",
    ) -> SinkWriteReceipt:
        """Verify grant and return deterministic receipt."""
        enforce_outbox_grant(sink_name=self.sink_name, item=item, grant=grant, source=source)
        return SinkWriteReceipt(
            destination_uri=f"https://api.atlassian.com/issue/{item.target_ref}/worklog",
            http_status=HTTP_CREATED,
            external_receipt_id="cli-wl-1",
        )


# endregion


# region OpenAPI & CLI Extension Tests
def test_openapi_31_schema_export_is_deterministic_warning_free_and_complete(
    tmp_path: Path,
) -> None:
    """Verify OpenAPI 3.1 export has zero warnings, all schemas, and deterministic bytes."""
    warnings = validate_openapi_schema_zero_warnings()
    assert warnings == []

    first_text = export_openapi_schema()
    second_text = export_openapi_schema()
    assert first_text == second_text and first_text.endswith("\n")

    parsed = json.loads(first_text)
    assert parsed["openapi"] == "3.1.0"
    schemas = set(parsed["components"]["schemas"].keys())
    missing_schemas = EXPECTED_COMPONENT_SCHEMAS - schemas
    assert missing_schemas == set()

    out_file = tmp_path / "openapi.json"
    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = schema_export_main(["--output", str(out_file)], stdout=out_buf, stderr=err_buf)
    assert rc == EXIT_SUCCESS
    assert out_file.read_text(encoding="utf-8") == first_text

    if DEFAULT_SCHEMA_OUTPUT_PATH.exists():
        assert DEFAULT_SCHEMA_OUTPUT_PATH.read_text(encoding="utf-8") == first_text


def test_wt_outbox_cli_workflow_and_ipc_provider_live_outbox_summary(tmp_path: Path) -> None:
    """Verify `wt outbox {preview,list,approve,reject,flush,ledger}` and IPC `outbox_summary`."""
    assert {"outbox", "jira", "serve"} <= CANONICAL_CLI_SUBCOMMANDS
    assert len(CANONICAL_CLI_SUBCOMMANDS) <= CANONICAL_LABEL_VALUE_CEILING

    from worktrack.cli.commands_outbox import use_cli_sinks

    env = _cli_env(tmp_path)
    assert (
        _invoke_wt(
            ["in", "Ship API", "--ref", "PROJ-777", "--at", "2026-11-07T08:00:00Z"],
            env,
        )[0]
        == EXIT_SUCCESS
    )
    assert _invoke_wt(["out", "--at", "2026-11-07T09:00:00Z"], env)[0] == EXIT_SUCCESS

    rc_prev, human_prev, _ = _invoke_wt(["outbox", "preview", "--sink", "jira"], env)
    assert rc_prev == EXIT_SUCCESS and "PROJ-777" in human_prev

    rc_stage, stage_json, _ = _invoke_wt(
        ["outbox", "preview", "--sink", "jira", "--stage", "--cadence", "manual", "--json"],
        env,
    )
    assert rc_stage == EXIT_SUCCESS
    staged_id = str(json.loads(stage_json)["staged_items"][0]["id"])

    provider = LedgerIpcServiceProvider(Path(env["WORKTRACK_DB_PATH"]))
    live_summary = provider.outbox_summary()
    assert live_summary.staged_count == 1 and live_summary.pending_seconds == 3600

    assert _invoke_wt(["outbox", "list"], env)[0] == EXIT_SUCCESS
    assert _invoke_wt(["outbox", "approve", staged_id], env)[0] == EXIT_SUCCESS

    with use_cli_sinks((_CliStubSink(),)):
        rc_flush, flush_json, _ = _invoke_wt(
            ["outbox", "flush", "--sink", "jira", "--json"],
            env,
        )
    assert rc_flush == EXIT_SUCCESS
    assert json.loads(flush_json)["results"][0]["outcome"] == "ok"

    rc_ledger, ledger_json, _ = _invoke_wt(["outbox", "ledger", "--json"], env)
    assert rc_ledger == EXIT_SUCCESS and len(json.loads(ledger_json)["records"]) == 1


def test_wt_jira_status_auth_url_and_sync_cli(tmp_path: Path) -> None:
    """Verify `wt jira {status,auth-url,sync}` local token isolation and issue sync."""
    from worktrack.cli.commands_web import use_jira_transport

    env = {
        **_cli_env(tmp_path),
        "WORKTRACK_JIRA_CLOUD_ID": "cloud-123",
        "WORKTRACK_JIRA_CLIENT_ID": "wt-jira-public-id",
        "WORKTRACK_JIRA_REDIRECT_URI": "http://127.0.0.1:5173/callback",
    }
    rc_unauth, unauth_json, _ = _invoke_wt(["jira", "status", "--json"], env)
    assert rc_unauth == EXIT_SUCCESS and json.loads(unauth_json)["authenticated"] is False

    rc_login, login_json, _ = _invoke_wt(
        [
            "jira",
            "login",
            "--access-token",
            SAMPLE_JIRA_ACCESS_TOKEN,
            "--scope",
            "read:jira-work write:jira-work",
            "--json",
        ],
        env,
    )
    assert rc_login == EXIT_SUCCESS and json.loads(login_json)["authenticated"] is True
    assert SAMPLE_JIRA_ACCESS_TOKEN not in login_json

    rc_auth, auth_json, _ = _invoke_wt(["jira", "status", "--json"], env)
    assert rc_auth == EXIT_SUCCESS and json.loads(auth_json)["authenticated"] is True
    assert SAMPLE_JIRA_ACCESS_TOKEN not in auth_json

    rc_url, url_json, _ = _invoke_wt(["jira", "auth-url", "--json"], env)
    parsed_url = json.loads(url_json)
    assert rc_url == EXIT_SUCCESS and "code_challenge=" in parsed_url["authorization_url"]
    assert len(str(parsed_url["code_verifier"])) >= 43

    with use_jira_transport(_FakeJiraTransport()):
        rc_sync, sync_json, _ = _invoke_wt(["jira", "sync", "--json"], env)
    assert rc_sync == EXIT_SUCCESS and json.loads(sync_json)["activities_created"] == 1

    rc_logout, logout_json, _ = _invoke_wt(["jira", "logout", "--json"], env)
    assert rc_logout == EXIT_SUCCESS and json.loads(logout_json)["removed"] is True
    rc_after, after_json, _ = _invoke_wt(["jira", "status", "--json"], env)
    assert rc_after == EXIT_SUCCESS and json.loads(after_json)["authenticated"] is False


def test_wt_serve_loopback_wsgi_and_readiness_check(tmp_path: Path) -> None:
    """Verify `wt serve` loopback WSGI server and `--check` readiness flag."""
    from worktrack.cli.commands_web import serve_wsgi_until_stopped

    env = _cli_env(tmp_path)
    ready_port: list[int] = []
    ready_event = threading.Event()
    stop_event = threading.Event()

    def _on_bound(bound_port: int) -> None:
        """Record ephemeral TCP port and signal server readiness."""
        ready_port.append(bound_port)
        ready_event.set()

    server_thread = threading.Thread(
        target=lambda: serve_wsgi_until_stopped(
            host="127.0.0.1",
            port=0,
            stop_event=stop_event,
            on_bound=_on_bound,
            environ=env,
        ),
        daemon=True,
    )
    server_thread.start()
    assert ready_event.wait(timeout=3.0)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{ready_port[0]}/healthz", timeout=2.0
        ) as resp:
            assert resp.status == HTTP_OK
            assert json.loads(resp.read().decode("utf-8"))["status"] == "ok"
    finally:
        stop_event.set()
        server_thread.join(timeout=3.0)

    assert (Path(env["WORKTRACK_TELEMETRY_DIR"]) / "events.jsonl").is_file()
    rc_serve_check, serve_out, _ = _invoke_wt(["serve", "--check", "--json"], env)
    assert rc_serve_check == EXIT_SUCCESS and json.loads(serve_out)["ready"] is True


# endregion
