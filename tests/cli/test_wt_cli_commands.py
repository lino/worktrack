"""End-to-end behavioural tests for `wt` CLI capture, split, fact, insight, and metrics."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from worktrack.cli import EXIT_SUCCESS, main
from worktrack.core import FACET_TYPE_INT, Ledger

# region Constants
SLICE_ONE_MINUTES = 40
SLICE_TWO_MINUTES = 20
EXPECTED_SPLIT_COUNT = 2
EXPECTED_BUDGET_MINUTES = 60
EXPECTED_PRIORITY = 1
CUSTOM_FOCUS_SCORE = 5
# endregion


# region Fixtures
def _build_isolated_env(tmp_path: Path) -> dict[str, str]:
    """Create isolated XDG and Worktrack environment dictionary in `tmp_path`."""
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "WORKTRACK_DB_PATH": str(tmp_path / "data" / "ledger.sqlite3"),
        "WORKTRACK_SOCKET_PATH": str(tmp_path / "run" / "wt.sock"),
        "WORKTRACK_TELEMETRY_DIR": str(tmp_path / "telemetry"),
    }


def _invoke_wt(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """Invoke `worktrack.cli.main` capturing exit code, stdout, and stderr."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    code = main(argv, environ=env, stdout=out_buf, stderr=err_buf)
    return code, out_buf.getvalue(), err_buf.getvalue()


# endregion


# region CLI Command Tests
def test_wt_in_and_out_resolve_activities_and_enforce_overlap_policies(tmp_path: Path) -> None:
    """Verify `wt in` creates/resolves activities, enforces policies, and `wt out` closes."""
    env = _build_isolated_env(tmp_path)
    rc_idle_out, _, err_idle = _invoke_wt(["out"], env)
    assert rc_idle_out != EXIT_SUCCESS and err_idle.strip() != ""

    rc_in, out_in, _ = _invoke_wt(
        [
            "in",
            "OAuth Token Broker",
            "--kind",
            "targeted",
            "--ref",
            "PROJ-501",
            "--notes",
            "S256",
            "--at",
            "2026-11-07T08:00:00Z",
            "--json",
        ],
        env,
    )
    assert rc_in == EXIT_SUCCESS
    created_act_id = json.loads(out_in)["activity"]["id"]

    assert _invoke_wt(["in", "Triage", "--at", "2026-11-07T08:30:00Z"], env)[0] != EXIT_SUCCESS
    rc_switch, _, _ = _invoke_wt(
        [
            "in",
            "Inbox Triage",
            "--kind",
            "standing",
            "--policy",
            "close_previous",
            "--at",
            "2026-11-07T08:30:00Z",
        ],
        env,
    )
    assert rc_switch == EXIT_SUCCESS

    rc_out, out_closed, _ = _invoke_wt(
        ["out", "--notes", "Inbox zero", "--at", "2026-11-07T09:00:00Z", "--json"], env
    )
    assert rc_out == EXIT_SUCCESS and json.loads(out_closed)["interval"]["notes"] == "Inbox zero"

    rc_reopen, out_reopen, _ = _invoke_wt(
        ["in", "PROJ-501", "--at", "2026-11-07T09:15:00Z", "--json"], env
    )
    assert rc_reopen == EXIT_SUCCESS
    assert json.loads(out_reopen)["activity"]["id"] == created_act_id
    assert _invoke_wt(["out", "--at", "2026-11-07T09:45:00Z"], env)[0] == EXIT_SUCCESS


def test_wt_log_chaotic_split_and_fact_assertions(tmp_path: Path) -> None:
    """Verify `wt log`, `wt split`, and `wt fact` capture, split, and assert ContextFacts."""
    env = _build_isolated_env(tmp_path)
    with Ledger(Path(env["WORKTRACK_DB_PATH"])) as ledger:
        ledger.register_facet("focus_score", FACET_TYPE_INT, description="Daily focus rating")

    rc_log, out_log, _ = _invoke_wt(
        [
            "log",
            "--start",
            "2026-11-07T13:00:00Z",
            "--end",
            "2026-11-07T14:00:00Z",
            "--tag",
            "deep",
            "--json",
        ],
        env,
    )
    assert rc_log == EXIT_SUCCESS
    interval_id = str(json.loads(out_log)["interval"]["id"])
    assert _invoke_wt(["fact", "workplace=office", "--interval", interval_id], env)[0] == 0

    rc_split, out_split, _ = _invoke_wt(
        [
            "split",
            interval_id,
            "--slice",
            f"Design Review:{SLICE_ONE_MINUTES}",
            "--slice",
            f"unallocated:{SLICE_TWO_MINUTES}",
            "--json",
        ],
        env,
    )
    assert rc_split == EXIT_SUCCESS
    slices = json.loads(out_split)["slices"]
    assert len(slices) == EXPECTED_SPLIT_COUNT
    assert slices[0]["activity_id"] is not None and slices[1]["activity_id"] is None

    assert _invoke_wt(["fact", "workplace=home_office"], env)[0] == EXIT_SUCCESS
    rc_int_fact, out_int_fact, _ = _invoke_wt(
        ["fact", f"focus_score={CUSTOM_FOCUS_SCORE}", "--date", "2026-11-06", "--json"], env
    )
    assert rc_int_fact == EXIT_SUCCESS
    assert json.loads(out_int_fact)["fact"]["value"] == CUSTOM_FOCUS_SCORE


def test_wt_agenda_subcommands_and_scheduled_counter(tmp_path: Path) -> None:
    """Verify `wt agenda {add,focus,list,done}` and `worktrack_agenda_items_scheduled_total`."""
    env = _build_isolated_env(tmp_path)
    rc_add, out_add, _ = _invoke_wt(
        [
            "agenda",
            "add",
            "Ship WT-WP-05",
            "--horizon",
            "today",
            "--priority",
            str(EXPECTED_PRIORITY),
            "--budget-minutes",
            str(EXPECTED_BUDGET_MINUTES),
            "--deadline",
            "2026-11-08",
            "--json",
        ],
        env,
    )
    assert rc_add == EXIT_SUCCESS
    item_id = str(json.loads(out_add)["item"]["id"])

    rc_focus, out_focus, _ = _invoke_wt(
        [
            "agenda",
            "focus",
            "--start",
            "2026-11-07T10:00:00Z",
            "--end",
            "2026-11-07T11:00:00Z",
            "--activity",
            "Ship WT-WP-05",
            "--expose-busy",
            "--json",
        ],
        env,
    )
    assert rc_focus == EXIT_SUCCESS and json.loads(out_focus)["focus_block"]["expose_busy"] is True
    assert _invoke_wt(["agenda", "list", "--horizon", "today"], env)[0] == EXIT_SUCCESS
    assert _invoke_wt(["agenda", "done", item_id], env)[0] == EXIT_SUCCESS

    _, exposition, _ = _invoke_wt(["metrics"], env)
    assert "worktrack_agenda_items_scheduled_total 2.0" in exposition
    assert 'worktrack_cli_commands_total{outcome="ok",subcommand="agenda"} 4.0' in exposition


def test_wt_horizon_insights_and_bounded_unknown_metric_label(tmp_path: Path) -> None:
    """Verify `wt now/today/week` human & JSON rendering and bounded `unknown` label."""
    env = _build_isolated_env(tmp_path)
    assert _invoke_wt(["in", "Ship WT-WP-05"], env)[0] == EXIT_SUCCESS
    for cmd in ("now", "today", "week"):
        assert _invoke_wt([cmd], env)[0] == EXIT_SUCCESS
        rc_j, out_j, _ = _invoke_wt(["--json", cmd], env)
        assert rc_j == EXIT_SUCCESS and "date_iso" in json.loads(out_j)

    assert _invoke_wt(["PROJ-9999"], env)[0] != EXIT_SUCCESS
    rc_metrics, exposition, _ = _invoke_wt(["metrics"], env)
    assert rc_metrics == EXIT_SUCCESS
    assert 'worktrack_cli_commands_total{outcome="error",subcommand="unknown"} 1.0' in exposition


def test_python_module_entrypoint_help(tmp_path: Path) -> None:
    """Verify `python -m worktrack.cli --help` executes cleanly and records `help` metric."""
    env = _build_isolated_env(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "worktrack.cli", "--help"],
        env={**env, "PYTHONPATH": "src"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "wt" in proc.stdout
    _, exposition, _ = _invoke_wt(["metrics"], env)
    assert 'worktrack_cli_commands_total{outcome="ok",subcommand="help"} 1.0' in exposition


# endregion
