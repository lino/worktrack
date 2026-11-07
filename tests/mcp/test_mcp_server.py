"""Behavioural tests for `WorktrackMcpServer` JSON-RPC 2.0 MCP tools and Outbox gate."""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worktrack.agenda import HORIZON_THIS_WEEK, HORIZON_TODAY, AgendaPlanner
from worktrack.core import ACTIVITY_KIND_TARGETED, Ledger
from worktrack.mcp import (
    DEFAULT_MCP_PROTOCOL_VERSION,
    MCP_ERR_EGRESS_BYPASS_BLOCKED,
    MCP_TOOL_AGENDA,
    MCP_TOOL_CLUES,
    MCP_TOOL_INSIGHTS,
    MCP_TOOL_PROPOSE_RETROSPLIT,
    MCP_TOOL_STAGE_OUTBOX_ROLLUP,
    MCP_TOOL_STATUS,
    McpEgressBypassError,
    McpRequestError,
    McpToolSpec,
    WorktrackMcpServer,
)
from worktrack.outbox import OUTBOX_STATUS_STAGED, OutboxStore
from worktrack.telemetry import TelemetryRecorder

# region Constants
FIXED_NOW = datetime(2026, 11, 7, 14, 0, 0, tzinfo=UTC)
ONE_HOUR_SECONDS = 3600
TWO_HOURS_SECONDS = 7200
BUDGET_NINETY_MINUTES = 90
PRIORITY_URGENT = 1
RETROSPLIT_EQUAL_COUNT = 2
EXPECTED_BYPASS_COUNT_TWO = 2.0
JSONRPC_INVALID_PARAMS_CODE = -32602
JSONRPC_METHOD_NOT_FOUND_CODE = -32601
JSONRPC_PARSE_ERROR_CODE = -32700
# endregion


# region Fixtures
@pytest.fixture
def mcp_env(tmp_path: Path) -> tuple[Path, TelemetryRecorder]:
    """Provide an isolated SQLite database path and `TelemetryRecorder`."""
    db_path = tmp_path / "mcp_node.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    return db_path, telemetry


def _seed_sample_workday(
    ledger: Ledger,
    planner: AgendaPlanner,
) -> tuple[str, str]:
    """Seed sample activities, intervals, workplace facts, and agenda items."""
    act_alpha = ledger.create_activity(
        "Implement MCP Server",
        kind=ACTIVITY_KIND_TARGETED,
        external_ref="WT-120",
        rollup_target_ref="WT-120",
    )
    act_beta = ledger.create_activity(
        "Write Tax Exporter",
        kind=ACTIVITY_KIND_TARGETED,
        external_ref="WT-121",
        rollup_target_ref="WT-121",
    )
    ledger.record_structured_interval(
        act_alpha.id,
        started_at=FIXED_NOW - timedelta(hours=3),
        ended_at=FIXED_NOW - timedelta(hours=1),
        notes="Private implementation note that must be redacted",
    )
    ledger.clock_in(
        act_beta.id,
        started_at=FIXED_NOW - timedelta(hours=1),
        notes="Active timer note",
    )
    ledger.assert_context_fact(
        "workplace",
        "home_office",
        scope_date=FIXED_NOW.date(),
        source="sensor",
    )
    planner.schedule_item(
        act_alpha.id,
        horizon=HORIZON_TODAY,
        priority=PRIORITY_URGENT,
        daily_budget_minutes=BUDGET_NINETY_MINUTES,
        deadline=FIXED_NOW.date().isoformat(),
    )
    planner.schedule_focus_block(
        started_at=FIXED_NOW - timedelta(minutes=30),
        ended_at=FIXED_NOW + timedelta(minutes=30),
        activity_id=act_beta.id,
        label="Deep Work: Exporters",
        expose_busy=True,
    )
    return act_alpha.id, act_beta.id


# endregion


# region MCP Protocol & Read Tool Tests
def test_mcp_initialize_and_tools_list_handshake(
    mcp_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify `initialize`, `notifications/initialized`, and `tools/list`."""
    db_path, telemetry = mcp_env
    with (
        Ledger(db_path, telemetry=telemetry) as ledger,
        OutboxStore(db_path, telemetry=telemetry) as outbox,
        AgendaPlanner(db_path, ledger=ledger, telemetry=telemetry) as planner,
    ):
        server = WorktrackMcpServer(
            ledger=ledger,
            outbox_store=outbox,
            agenda_planner=planner,
            telemetry=telemetry,
        )
        specs = server.list_tools()
        assert all(isinstance(spec, McpToolSpec) for spec in specs)
        tool_names = {spec.name for spec in specs}
        assert {
            MCP_TOOL_STATUS,
            MCP_TOOL_INSIGHTS,
            MCP_TOOL_AGENDA,
            MCP_TOOL_CLUES,
            MCP_TOOL_STAGE_OUTBOX_ROLLUP,
            MCP_TOOL_PROPOSE_RETROSPLIT,
        } <= tool_names

        init_resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": DEFAULT_MCP_PROTOCOL_VERSION},
            }
        )
        init_result = init_resp["result"]
        assert isinstance(init_result, dict)
        assert init_result["protocolVersion"] == DEFAULT_MCP_PROTOCOL_VERSION
        assert "tools" in init_result["capabilities"]

        notif_resp = server.handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert notif_resp["result"] == {"acknowledged": True}

        list_resp = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed_tools = list_resp["result"]["tools"]
        assert len(listed_tools) == len(specs)


def test_mcp_read_tools_status_insights_agenda_and_clues(
    mcp_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify read tools return accurate status, insights, agenda, and local clues."""
    db_path, telemetry = mcp_env
    with (
        Ledger(db_path, telemetry=telemetry) as ledger,
        OutboxStore(db_path, telemetry=telemetry) as outbox,
        AgendaPlanner(db_path, ledger=ledger, telemetry=telemetry) as planner,
    ):
        _, act_beta_id = _seed_sample_workday(ledger, planner)
        server = WorktrackMcpServer(
            ledger=ledger,
            outbox_store=outbox,
            agenda_planner=planner,
            telemetry=telemetry,
            clock=lambda: FIXED_NOW,
        )

        status_data = server.call_tool(MCP_TOOL_STATUS)
        assert status_data["active_activity"]["id"] == act_beta_id
        assert status_data["active_elapsed_seconds"] == ONE_HOUR_SECONDS
        assert status_data["today_total_seconds"] == TWO_HOURS_SECONDS + ONE_HOUR_SECONDS
        assert status_data["workplace"] == "home_office"

        for horizon_arg in ("now", HORIZON_TODAY, "week", HORIZON_THIS_WEEK):
            insight_data = server.call_tool(MCP_TOOL_INSIGHTS, {"horizon": horizon_arg})
            assert insight_data["date_iso"] == FIXED_NOW.date().isoformat()

        agenda_data = server.call_tool(MCP_TOOL_AGENDA, {"horizon": HORIZON_TODAY})
        assert len(agenda_data["agenda_items"]) == 1
        assert len(agenda_data["focus_blocks"]) == 1
        assert agenda_data["focus_blocks"][0]["label"] == "Deep Work: Exporters"

        clues_data = server.call_tool(MCP_TOOL_CLUES)
        assert clues_data["count"] == 0
        assert clues_data["clues"] == []


def test_mcp_clues_reads_local_sqlite_clues_table_when_present(
    mcp_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify `worktrack_clues` queries local `clues` rows when table is present."""
    db_path, telemetry = mcp_env
    with (
        Ledger(db_path, telemetry=telemetry) as ledger,
        closing(sqlite3.connect(db_path)) as raw_conn,
    ):
        with raw_conn:
            raw_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS clues (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    activity_ref TEXT
                )
                """
            )
            raw_conn.execute(
                "INSERT INTO clues (id, source, observed_at, summary, activity_ref) "
                "VALUES (?, ?, ?, ?, ?)",
                ("clue-1", "git", "2026-11-07T12:00:00.000000Z", "Commit WT-120", "WT-120"),
            )
        server = WorktrackMcpServer(ledger=ledger, telemetry=telemetry)
        clues_payload = server.call_tool(MCP_TOOL_CLUES, {"source": "git"})
        assert clues_payload["count"] == 1
        assert clues_payload["clues"][0]["summary"] == "Commit WT-120"


def test_mcp_stdio_stream_processing_and_jsonrpc_errors(
    mcp_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify line-delimited stdio stream processing and JSON-RPC error codes."""
    db_path, telemetry = mcp_env
    with Ledger(db_path, telemetry=telemetry) as ledger:
        server = WorktrackMcpServer(ledger=ledger, telemetry=telemetry, clock=lambda: FIXED_NOW)
        requests = [
            json.dumps({"jsonrpc": "2.0", "id": "req-1", "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "req-2",
                    "method": "tools/call",
                    "params": {"name": MCP_TOOL_STATUS, "arguments": {}},
                }
            ),
            "{not-valid-json",
            json.dumps({"jsonrpc": "2.0", "id": "req-3", "method": "unknown/method"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "req-4",
                    "method": "tools/call",
                    "params": {"name": "unknown_tool", "arguments": {}},
                }
            ),
        ]
        stdin_stream = io.StringIO("\n".join(requests) + "\n")
        stdout_stream = io.StringIO()
        processed = server.serve_stdio(stdin_stream, stdout_stream)
        assert processed == len(requests)

        output_lines = [
            json.loads(line) for line in stdout_stream.getvalue().splitlines() if line.strip()
        ]
        assert len(output_lines) == 5
        assert output_lines[0]["id"] == "req-1"
        assert output_lines[1]["id"] == "req-2"
        assert output_lines[1]["result"]["isError"] is False
        assert output_lines[2]["error"]["code"] == JSONRPC_PARSE_ERROR_CODE
        assert output_lines[3]["error"]["code"] == JSONRPC_METHOD_NOT_FOUND_CODE
        assert output_lines[4]["error"]["code"] == JSONRPC_INVALID_PARAMS_CODE


# endregion


# region MCP Outbox Gate Tests
def test_mcp_stage_outbox_rollup_stages_without_flushing(
    mcp_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify `worktrack_stage_outbox_rollup` stages previewable items with `status='staged'`."""
    db_path, telemetry = mcp_env
    with (
        Ledger(db_path, telemetry=telemetry) as ledger,
        OutboxStore(db_path, telemetry=telemetry) as outbox,
        AgendaPlanner(db_path, ledger=ledger, telemetry=telemetry) as planner,
    ):
        _seed_sample_workday(ledger, planner)
        server = WorktrackMcpServer(
            ledger=ledger,
            outbox_store=outbox,
            agenda_planner=planner,
            telemetry=telemetry,
            clock=lambda: FIXED_NOW,
        )
        rpc_resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": "stage-1",
                "method": "tools/call",
                "params": {
                    "name": MCP_TOOL_STAGE_OUTBOX_ROLLUP,
                    "arguments": {
                        "sink": "jira",
                        "grouping_window": "day",
                        "redact_notes": True,
                    },
                },
            }
        )
        structured = rpc_resp["result"]["structuredContent"]
        assert structured["sink"] == "jira"
        assert structured["status"] == OUTBOX_STATUS_STAGED
        assert structured["staged_count"] == 1

        staged_item = structured["staged_items"][0]
        assert staged_item["status"] == OUTBOX_STATUS_STAGED
        assert staged_item["target_ref"] == "WT-120"
        assert staged_item["rounded_duration_seconds"] == TWO_HOURS_SECONDS
        assert "Private implementation note" not in staged_item["summary"]

        persisted = outbox.list_items(sink="jira")
        assert len(persisted) == 1
        assert persisted[0].status == OUTBOX_STATUS_STAGED
        assert outbox.list_egress_records() == []


def test_mcp_propose_retrosplit_returns_non_destructive_preview(
    mcp_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify `worktrack_propose_retrosplit` previews slice allocations without mutating."""
    db_path, telemetry = mcp_env
    with Ledger(db_path, telemetry=telemetry) as ledger:
        act_one = ledger.create_activity("Task One", external_ref="WT-1")
        act_two = ledger.create_activity("Task Two", external_ref="WT-2")
        chaotic = ledger.record_chaotic_interval(
            started_at=FIXED_NOW - timedelta(hours=2),
            ended_at=FIXED_NOW,
        )
        server = WorktrackMcpServer(ledger=ledger, telemetry=telemetry)
        proposal = server.call_tool(
            MCP_TOOL_PROPOSE_RETROSPLIT,
            {
                "interval_id": chaotic.id,
                "activity_ids": [act_one.id, act_two.id],
            },
        )
        assert proposal["interval_id"] == chaotic.id
        assert len(proposal["slices"]) == RETROSPLIT_EQUAL_COUNT
        assert proposal["slices"][0]["duration_seconds"] == ONE_HOUR_SECONDS
        assert proposal["slices"][1]["duration_seconds"] == ONE_HOUR_SECONDS
        assert ledger.get_interval(chaotic.id).activity_id is None

        weighted = server.call_tool(
            MCP_TOOL_PROPOSE_RETROSPLIT,
            {
                "interval_id": chaotic.id,
                "activity_ids": [act_one.id, act_two.id],
                "weights": [0.25, 0.75],
            },
        )
        assert (
            weighted["slices"][0]["duration_seconds"] == ONE_HOUR_SECONDS / RETROSPLIT_EQUAL_COUNT
        )
        assert weighted["slices"][1]["duration_seconds"] == (
            TWO_HOURS_SECONDS - ONE_HOUR_SECONDS / RETROSPLIT_EQUAL_COUNT
        )

        ping_resp = server.handle_request({"jsonrpc": "2.0", "id": "ping-1", "method": "ping"})
        assert ping_resp["result"] == {}


def test_mcp_blocks_direct_sink_write_and_auto_flush_bypass_attempts(
    mcp_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify direct Sink write tools and `auto_flush=True` bypasses are blocked and metered."""
    db_path, telemetry = mcp_env
    with (
        Ledger(db_path, telemetry=telemetry) as ledger,
        OutboxStore(db_path, telemetry=telemetry) as outbox,
        AgendaPlanner(db_path, ledger=ledger, telemetry=telemetry) as planner,
    ):
        _seed_sample_workday(ledger, planner)
        server = WorktrackMcpServer(
            ledger=ledger,
            outbox_store=outbox,
            agenda_planner=planner,
            telemetry=telemetry,
        )

        with pytest.raises(McpEgressBypassError):
            server.call_tool("worktrack_direct_sink_write", {"sink": "jira"})

        bypass_resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": "bypass-1",
                "method": "tools/call",
                "params": {
                    "name": MCP_TOOL_STAGE_OUTBOX_ROLLUP,
                    "arguments": {"sink": "jira", "auto_flush": True},
                },
            }
        )
        error_obj = bypass_resp["error"]
        assert error_obj["code"] == MCP_ERR_EGRESS_BYPASS_BLOCKED
        assert error_obj["data"]["error_code"] == "egress_bypass_blocked"
        assert error_obj["data"]["sink"] == "jira"
        assert error_obj["data"]["source"] == "mcp"

        assert outbox.list_items() == []
        assert outbox.list_egress_records() == []

        metrics_text = telemetry.render_metrics_text()
        assert (
            f'worktrack_egress_bypass_blocked_total{{sink="jira",source="mcp"}} '
            f"{EXPECTED_BYPASS_COUNT_TWO}"
        ) in metrics_text

        with pytest.raises(McpRequestError):
            server.call_tool(MCP_TOOL_INSIGHTS, {"horizon": "invalid_horizon"})


# endregion
