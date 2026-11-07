"""Behavioural tests for Wave 2-4 `wt` CLI subcommands and telemetry label ceilings."""

from __future__ import annotations

import io
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

import pytest

from worktrack.cli import CANONICAL_CLI_SUBCOMMANDS, EXIT_SUCCESS, main
from worktrack.collectors import ClueStore, GitCommitRecord
from worktrack.core import Ledger
from worktrack.hub_sync import HubRollupIngestStore, LoopbackHubSyncTransport
from worktrack.sensors import WorkstationSnapshot
from worktrack.telemetry.catalog import (
    CANONICAL_LABEL_VALUE_CEILING,
    CANONICAL_METRIC_SPECS,
    DEFAULT_SERIES_LABEL_FLOOR,
    TelemetryCardinalityError,
    enforce_series_budgets,
)
from worktrack.telemetry.persistence import encode_label_values

# region Constants
SAMPLE_ICS_PAYLOAD = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Worktrack//Test//EN
BEGIN:VEVENT
UID:evt-cli-601@example.test
SUMMARY:PROJ-601 Architecture Sync
DTSTART:20261107T090000Z
DTEND:20261107T093000Z
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""
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


def _invoke_wt(argv: list[str] | str, env: dict[str, str]) -> tuple[int, str, str]:
    """Invoke `wt` CLI `main` with in-memory `stdout` and `stderr` streams."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    resolved_argv = shlex.split(argv) if isinstance(argv, str) else argv
    rc = main(resolved_argv, environ=env, stdout=out_buf, stderr=err_buf)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class _FakeGitLogRunner:
    """Deterministic `GitLogRunner` returning a single commit referencing `PROJ-401`."""

    def list_commits(
        self,
        repo_path: Path,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        author_pattern: str | None = None,
        max_commits: int = 200,
    ) -> list[GitCommitRecord]:
        """Return one synthetic commit record."""
        _ = (since, until, author_pattern, max_commits)
        return [
            GitCommitRecord(
                commit_sha="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
                author_name="Ada Lovelace",
                author_email="ada@example.test",
                occurred_at=datetime(2026, 11, 7, 8, 30, tzinfo=UTC),
                subject="PROJ-401 Wire CLI clue collector",
                repository=repo_path.name,
                branch="main",
            )
        ]


class _FakeWorkstationProbe:
    """Deterministic `WorkstationProbe` returning a home-office Wi-Fi snapshot."""

    def probe(self) -> WorkstationSnapshot:
        """Return fixed `WorkstationSnapshot`."""
        return WorkstationSnapshot(
            captured_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
            ip_addresses=("192.168.178.42",),
            wifi_ssid="Home-WLAN",
            active_interfaces=("en0",),
            display_count=1,
            hostname="workstation.local",
        )


# endregion


# region Telemetry Tests
def test_enforce_series_budgets_expands_canonical_ceiling_while_respecting_strict_test_cap() -> (
    None
):
    """Verify `enforce_series_budgets` allows up to 32 canonical labels at default floor."""
    expected_new = {"clue", "sensor", "reconcile", "calendar", "mcp", "hub", "export"}
    assert expected_new <= CANONICAL_CLI_SUBCOMMANDS
    assert len(CANONICAL_CLI_SUBCOMMANDS) <= CANONICAL_LABEL_VALUE_CEILING

    cli_spec = CANONICAL_METRIC_SPECS["worktrack_cli_commands_total"]
    existing: set[str] = set()
    labels = [*sorted(CANONICAL_CLI_SUBCOMMANDS)]
    labels.extend(f"extra_{i}" for i in range(len(labels), CANONICAL_LABEL_VALUE_CEILING))
    for sub in labels:
        tup = ("ok", sub)
        key = encode_label_values(tup)
        enforce_series_budgets(cli_spec, existing, key, tup, DEFAULT_SERIES_LABEL_FLOOR, 256)
        existing.add(key)

    overflow_tup = ("ok", "overflow_subcommand")
    with pytest.raises(TelemetryCardinalityError):
        enforce_series_budgets(
            cli_spec, existing, encode_label_values(overflow_tup), overflow_tup, 20, 256
        )

    strict_existing = {encode_label_values(("ok", f"s{i}")) for i in range(3)}
    with pytest.raises(TelemetryCardinalityError):
        enforce_series_budgets(
            cli_spec, strict_existing, encode_label_values(("ok", "s3")), ("ok", "s3"), 3, 256
        )


# endregion


# region Wave 2-4 CLI Tests
def test_wt_clue_git_sync_and_list_cli(tmp_path: Path) -> None:
    """Verify `wt clue git-sync` and `wt clue list` sync and query ambient Git clues."""
    from worktrack.cli.commands_clues_sensors import use_git_log_runner

    env = _cli_env(tmp_path)
    repo_dir = tmp_path / "worktrack-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    assert (
        _invoke_wt(
            ["in", "Ship Clues", "--ref", "PROJ-401", "--at", "2026-11-07T08:00:00Z"],
            env,
        )[0]
        == EXIT_SUCCESS
    )
    assert _invoke_wt(["out", "--at", "2026-11-07T09:00:00Z"], env)[0] == EXIT_SUCCESS

    sync_cmd = (
        f"clue git-sync --repo '{repo_dir}' "
        "--since 2026-11-07T00:00:00Z --until 2026-11-07T23:59:59Z "
        "--author ada@example.test --json"
    )
    with use_git_log_runner(_FakeGitLogRunner()):
        rc_sync, sync_out, _ = _invoke_wt(sync_cmd, env)
    assert rc_sync == EXIT_SUCCESS
    sync_payload = json.loads(sync_out)
    assert sync_payload["clues_upserted"] == 1 and sync_payload["activities_linked"] == 1

    list_cmd = (
        "clue list --source git --start 2026-11-07T00:00:00Z --end 2026-11-07T23:59:59Z --json"
    )
    rc_list, list_out, _ = _invoke_wt(list_cmd, env)
    assert rc_list == EXIT_SUCCESS
    clues = json.loads(list_out)["clues"]
    assert len(clues) == 1 and clues[0]["inferred_activity_key"] == "PROJ-401"

    rc_human, human_out, _ = _invoke_wt(["clue", "list"], env)
    assert rc_human == EXIT_SUCCESS and "PROJ-401" in human_out


def test_wt_sensor_evaluate_and_apply_cli(tmp_path: Path) -> None:
    """Verify `wt sensor evaluate --apply` records a clue and asserts `workplace` fact."""
    from worktrack.cli.commands_clues_sensors import use_workstation_probe

    env = _cli_env(tmp_path)
    rules_json = json.dumps(
        [{"rule_id": "home-wlan", "workplace": "home_office", "wifi_ssids": ["Home-WLAN"]}]
    )
    with use_workstation_probe(_FakeWorkstationProbe()):
        rc, out, _ = _invoke_wt(
            [
                "sensor",
                "evaluate",
                "--date",
                "2026-11-07",
                "--apply",
                "--rules-json",
                rules_json,
                "--json",
            ],
            env,
        )
    assert rc == EXIT_SUCCESS
    payload = json.loads(out)
    assert payload["workplace"] == "home_office"
    assert payload["clue_id"] is not None and payload["context_fact_id"] is not None


def test_wt_reconcile_inspect_propose_and_apply_cli(tmp_path: Path) -> None:
    """Verify `wt reconcile {inspect,propose,apply}` gap inspection and RetroSplit execution."""
    env = _cli_env(tmp_path)
    db_path = Path(env["WORKTRACK_DB_PATH"])
    with Ledger(db_path) as ledger, ClueStore(db_path) as clue_store:
        act_a = ledger.create_activity("Feature A", external_ref="PROJ-501")
        act_b = ledger.create_activity("Feature B", external_ref="PROJ-502")
        chaotic = ledger.record_chaotic_interval(
            started_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
            ended_at=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
            notes="Morning sprint block",
        )
        for sha, ref, aid, minute in (
            ("sha-501", "PROJ-501", act_a.id, 15),
            ("sha-502", "PROJ-502", act_b.id, 45),
        ):
            clue_store.upsert_clue(
                source="git",
                external_key=sha,
                occurred_at=datetime(2026, 11, 7, 10, minute, tzinfo=UTC),
                summary=f"{ref} commit",
                inferred_activity_key=ref,
                activity_id=aid,
                confidence=0.9,
            )

    insp_cmd = "reconcile inspect --start 2026-11-07T00:00:00Z --end 2026-11-07T23:59:59Z --json"
    rc_insp, insp_out, _ = _invoke_wt(insp_cmd, env)
    assert rc_insp == EXIT_SUCCESS
    insp_data = json.loads(insp_out)
    assert insp_data["total_unallocated_seconds"] == 3600 and len(insp_data["candidate_clues"]) == 2

    prop_cmd = (
        f"reconcile propose --interval-id {chaotic.id} "
        "--activity PROJ-501 --activity PROJ-502 "
        "--strategy weights --weights 1 --weights 3 --json"
    )
    rc_prop, prop_out, _ = _invoke_wt(prop_cmd, env)
    assert rc_prop == EXIT_SUCCESS
    slices = json.loads(prop_out)["proposal"]["slices"]
    assert [s["duration_seconds"] for s in slices] == [900, 2700]

    apply_cmd = (
        f"reconcile apply --interval-id {chaotic.id} "
        "--activity PROJ-501 --activity PROJ-502 --strategy equal --json"
    )
    rc_apply, apply_out, _ = _invoke_wt(apply_cmd, env)
    assert rc_apply == EXIT_SUCCESS
    created = json.loads(apply_out)["created_intervals"]
    assert len(created) == 2 and [c["duration_seconds"] for c in created] == [1800, 1800]


def test_wt_calendar_sync_and_stage_focus_cli(tmp_path: Path) -> None:
    """Verify `wt calendar sync` and `wt calendar stage-focus` CLI subcommands."""
    env = _cli_env(tmp_path)
    ics_file = tmp_path / "meetings.ics"
    ics_file.write_text(SAMPLE_ICS_PAYLOAD, encoding="utf-8")

    sync_cmd = (
        f"calendar sync --ics-file '{ics_file}' --standing-activity 'Meetings & Alignment' --json"
    )
    rc_sync, sync_out, _ = _invoke_wt(sync_cmd, env)
    assert rc_sync == EXIT_SUCCESS
    sync_data = json.loads(sync_out)
    assert sync_data["events_synced"] == 1 and sync_data["intervals_created"] == 1

    focus_cmd = (
        "agenda focus --start 2026-11-07T13:00:00Z --end 2026-11-07T14:00:00Z "
        "--label 'Private Deep Work' --expose-busy"
    )
    assert _invoke_wt(focus_cmd, env)[0] == EXIT_SUCCESS

    stage_cmd = (
        "calendar stage-focus --start 2026-11-07T00:00:00Z "
        "--end 2026-11-07T23:59:59Z --target-ref primary --json"
    )
    rc_stage, stage_out, _ = _invoke_wt(stage_cmd, env)
    assert rc_stage == EXIT_SUCCESS
    staged = json.loads(stage_out)["staged_items"]
    assert len(staged) == 1 and staged[0]["sink"] == "calendar"
    assert staged[0]["payload"]["busy_summary"] == "Focus Time"


def test_wt_mcp_once_stdio_and_hub_status_pull_catalog_cli(tmp_path: Path) -> None:
    """Verify `wt mcp --once`, `wt hub status`, and `wt hub pull-catalog` CLI commands."""
    from worktrack.cli.commands_calendar_mcp_hub_export import use_cli_stdin, use_hub_transport

    env = _cli_env(tmp_path)
    rpc_line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    with use_cli_stdin(io.StringIO(rpc_line + "\n")):
        rc_mcp, mcp_out, _ = _invoke_wt(["mcp", "--once"], env)
    assert rc_mcp == EXIT_SUCCESS
    tools = json.loads(mcp_out.strip())["result"]["tools"]
    tool_names = {t["name"] for t in tools}
    assert "worktrack_status" in tool_names and "worktrack_clues" in tool_names

    hub_db = tmp_path / "hub.sqlite3"
    with HubRollupIngestStore(hub_db) as hub_store:
        hub_store.upsert_catalog_entity("client", "acme", "Acme Corp")
        hub_store.ingest_rollup(
            {
                "node_id": "node-alpha",
                "idempotency_key": "idem-1",
                "target_ref": "hub:team-core",
                "window_start": "2026-11-07T00:00:00Z",
                "window_end": "2026-11-08T00:00:00Z",
                "rounded_duration_seconds": 3600,
                "entity_shares": {"client:acme": 1.0},
            }
        )
        with use_hub_transport(LoopbackHubSyncTransport(ingest_store=hub_store)):
            rc_pull, pull_out, _ = _invoke_wt(
                ["hub", "pull-catalog", "--hub-url", "https://hub.example.test", "--json"],
                env,
            )
    assert rc_pull == EXIT_SUCCESS
    assert json.loads(pull_out)["entities_created"] == 1

    with HubRollupIngestStore(Path(env["WORKTRACK_DB_PATH"])) as local_hub:
        local_hub.upsert_catalog_entity("team", "platform", "Platform Engineering")
    rc_status, status_out, _ = _invoke_wt(["hub", "status", "--json"], env)
    assert rc_status == EXIT_SUCCESS
    assert json.loads(status_out)["catalog_entities_count"] == 1


def test_wt_export_tax_and_attribution_cli(tmp_path: Path) -> None:
    """Verify `wt export tax` and `wt export attribution` across formats and `--json`."""
    env = _cli_env(tmp_path)
    db_path = Path(env["WORKTRACK_DB_PATH"])
    with Ledger(db_path) as ledger:
        act = ledger.create_activity("Platform API", external_ref="PROJ-701")
        ent = ledger.create_entity("client", "globex", "Globex Corp")
        ledger.set_attribution(target_entity_id=ent.id, weight=1.0, source_activity_id=act.id)
        ledger.record_structured_interval(
            act.id,
            started_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
            ended_at=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
        )
        ledger.assert_context_fact("workplace", "home_office", scope_date="2026-11-07")

    rc_tax, tax_out, _ = _invoke_wt(
        ["export", "tax", "--year", "2026", "--format", "markdown"],
        env,
    )
    assert rc_tax == EXIT_SUCCESS and "home_office" in tax_out

    rc_tax_json, tax_json, _ = _invoke_wt(
        ["export", "tax", "--year", "2026", "--format", "csv", "--json"],
        env,
    )
    assert rc_tax_json == EXIT_SUCCESS
    assert json.loads(tax_json)["report"]["workplace_days"]["home_office"] == 1

    rc_attr, attr_out, _ = _invoke_wt(
        [
            "export",
            "attribution",
            "--start",
            "2026-11-07T00:00:00Z",
            "--end",
            "2026-11-07T23:59:59Z",
            "--format",
            "csv",
            "--kind",
            "client",
            "--json",
        ],
        env,
    )
    assert rc_attr == EXIT_SUCCESS
    attr_payload = json.loads(attr_out)
    assert attr_payload["report"]["rows"][0]["entity_key"] == "globex"
    assert "globex" in attr_payload["rendered"]


# endregion
