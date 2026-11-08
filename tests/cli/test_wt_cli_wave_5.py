"""Behavioural tests for Wave 5 CLI subcommands (wt absence, travel, report)."""

from __future__ import annotations

import io
import json
import shlex
from pathlib import Path

from worktrack.cli import EXIT_SUCCESS, main
from worktrack.core import Ledger

# region Constants
DATE_VACATION_START = "2026-08-03"
DATE_VACATION_END = "2026-08-07"
DATE_HALF_DAY = "2026-08-10"
YEAR_2026 = 2026
DEFAULT_ENTITLEMENT = 30.0
CUSTOM_CARRYOVER = 2.5
DISTANCE_KM_350 = 350.0
COMMUTE_KM_25 = 25.0
SUBDIVISION_BAVARIA = "DE-BY"
PACK_GERMANY = "de"
PACK_GENERIC = "generic"
# endregion


# region Fixtures
def _cli_env(tmp_path: Path) -> dict[str, str]:
    """Create isolated environment dictionary for CLI test invocations."""
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
    """Invoke CLI main with captured in-memory stdout and stderr buffers."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    resolved_argv = shlex.split(argv) if isinstance(argv, str) else argv
    code = main(resolved_argv, environ=env, stdout=out_buf, stderr=err_buf)
    return code, out_buf.getvalue(), err_buf.getvalue()


# endregion


# region Tests
def test_cli_absence_quota_and_holidays(tmp_path: Path) -> None:
    """Verify wt absence quota and holidays subcommands."""
    env = _cli_env(tmp_path)

    # 1. Quota set & retrieve
    code, out, _ = _invoke_wt(
        f"absence quota --year {YEAR_2026} --entitlement {DEFAULT_ENTITLEMENT} "
        f"--carryover {CUSTOM_CARRYOVER} --json",
        env,
    )
    assert code == EXIT_SUCCESS
    quota_payload = json.loads(out)
    assert quota_payload["year"] == YEAR_2026
    assert quota_payload["total_available_days"] == DEFAULT_ENTITLEMENT + CUSTOM_CARRYOVER

    code, out, _ = _invoke_wt(f"absence quota --year {YEAR_2026}", env)
    assert code == EXIT_SUCCESS
    assert str(YEAR_2026) in out

    # 2. Absence holidays
    code, out, _ = _invoke_wt(
        f"absence holidays --year {YEAR_2026} --subdivision {SUBDIVISION_BAVARIA} --json",
        env,
    )
    assert code == EXIT_SUCCESS
    holidays = json.loads(out)
    assert any(h["name"] == "Neujahr" for h in holidays)


def test_cli_absence_add_list_and_summary(tmp_path: Path) -> None:
    """Verify wt absence add, list, and summary subcommands."""
    env = _cli_env(tmp_path)

    # Set quota first so summary has total quota
    _invoke_wt(
        f"absence quota --year {YEAR_2026} --entitlement {DEFAULT_ENTITLEMENT} "
        f"--carryover {CUSTOM_CARRYOVER} --json",
        env,
    )

    # 1. Absence add (full week vacation)
    code, out, _ = _invoke_wt(
        f"absence add --start {DATE_VACATION_START} --end {DATE_VACATION_END} "
        f"--kind vacation --notes 'Summer leave' --subdivision {SUBDIVISION_BAVARIA} --json",
        env,
    )
    assert code == EXIT_SUCCESS
    absence_payload = json.loads(out)
    assert absence_payload["effective_days"] == 5.0

    # 2. Absence add (half day compensatory off)
    code, out, _ = _invoke_wt(
        f"absence add --start {DATE_HALF_DAY} --kind compensatory_off "
        f"--fraction 0.5 --subdivision {SUBDIVISION_BAVARIA} --json",
        env,
    )
    assert code == EXIT_SUCCESS
    half_day_payload = json.loads(out)
    assert half_day_payload["effective_days"] == 0.5

    # 3. Absence list
    code, out, _ = _invoke_wt("absence list --start 2026-08-01 --end 2026-08-31 --json", env)
    assert code == EXIT_SUCCESS
    items = json.loads(out)
    assert len(items) == 2

    # 4. Absence summary
    code, out, _ = _invoke_wt(
        f"absence summary --year {YEAR_2026} --reference-date 2026-08-05 "
        f"--subdivision {SUBDIVISION_BAVARIA} --json",
        env,
    )
    assert code == EXIT_SUCCESS
    summary = json.loads(out)
    assert summary["year"] == YEAR_2026
    assert summary["total_quota_days"] == DEFAULT_ENTITLEMENT + CUSTOM_CARRYOVER
    assert summary["taken_vacation_days"] == 3.0
    assert summary["planned_vacation_days"] == 2.0


def test_cli_travel_workflow(tmp_path: Path) -> None:
    """Verify wt travel add and list with per-diem and mileage calculations."""
    env = _cli_env(tmp_path)

    # 1. Travel add
    code, out, _ = _invoke_wt(
        f"travel add --title 'Client Workshop' --destination Munich "
        f"--start 2026-10-12T08:00:00Z --end 2026-10-14T18:00:00Z "
        f"--mode car --km {DISTANCE_KM_350} "
        f"--breakfast 2026-10-13,2026-10-14 --lunch 2026-10-13 "
        f"--pack {PACK_GERMANY} --json",
        env,
    )
    assert code == EXIT_SUCCESS
    calc = json.loads(out)
    assert calc["trip"]["title"] == "Client Workshop"
    assert calc["mileage_allowance"] == 105.0  # 350 * 0.30
    assert calc["gross_per_diem_total"] == 56.0  # 14 + 28 + 14
    assert calc["net_per_diem_total"] == 33.6  # 56 - deductions (5.6 + 11.2 + 5.6)
    assert calc["total_reimbursement"] == 138.6

    # 2. Travel list
    code, out, _ = _invoke_wt(
        f"travel list --start 2026-10-01 --end 2026-10-31 --pack {PACK_GERMANY} --json",
        env,
    )
    assert code == EXIT_SUCCESS
    trips = json.loads(out)
    assert len(trips) == 1
    assert trips[0]["trip"]["destination"] == "Munich"


def test_cli_report_packs_and_schema(tmp_path: Path) -> None:
    """Verify wt report packs and wt report schema subcommands."""
    env = _cli_env(tmp_path)

    # 1. Report packs
    code, out, _ = _invoke_wt("report packs --json", env)
    assert code == EXIT_SUCCESS
    packs = json.loads(out)
    slugs = [p["slug"] for p in packs]
    assert PACK_GERMANY in slugs
    assert PACK_GENERIC in slugs

    # 2. Report schema
    code_s1, out_s1, _ = _invoke_wt("report schema --kind json-schema", env)
    assert code_s1 == EXIT_SUCCESS
    schema1 = json.loads(out_s1)
    assert "$schema" in schema1

    code_s2, out_s2, _ = _invoke_wt("report schema --kind csvw", env)
    assert code_s2 == EXIT_SUCCESS
    schema2 = json.loads(out_s2)
    assert "@context" in schema2


def test_cli_report_generate_workflow(tmp_path: Path) -> None:
    """Verify wt report generate subcommand across JSON, Markdown, and CSV."""
    env = _cli_env(tmp_path)
    db_path = Path(env["WORKTRACK_DB_PATH"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with Ledger(db_path) as ledger:
        ledger.create_activity("Client Engineering")
        # Assert workplace facts: 2 home office, 1 office
        ledger.assert_context_fact("workplace", "home_office", scope_date="2026-09-01")
        ledger.assert_context_fact("workplace", "home_office", scope_date="2026-09-02")
        ledger.assert_context_fact("workplace", "office", scope_date="2026-09-03")

    # 1. Report generate (JSON)
    code, out, _ = _invoke_wt(
        f"report generate --start 2026-09-01 --end 2026-09-30 --pack {PACK_GERMANY} "
        f"--subdivision {SUBDIVISION_BAVARIA} --one-way-km {COMMUTE_KM_25} --format json",
        env,
    )
    assert code == EXIT_SUCCESS
    summary = json.loads(out)
    assert summary["home_office_days_raw"] == 2
    assert summary["office_days_raw"] == 1
    assert summary["home_office_allowance"] == 12.0  # 2 * 6.00
    assert summary["commute_allowance_total"] == 7.9  # 20*0.30 + 5*0.38

    # 2. Report generate (Markdown and CSV)
    code_md, out_md, _ = _invoke_wt(
        f"report generate --start 2026-09-01 --end 2026-09-30 "
        f"--pack {PACK_GERMANY} --format markdown",
        env,
    )
    assert code_md == EXIT_SUCCESS
    assert "# Jurisdiction Report:" in out_md

    code_csv, out_csv, _ = _invoke_wt(
        f"report generate --start 2026-09-01 --end 2026-09-30 --pack {PACK_GERMANY} --format csv",
        env,
    )
    assert code_csv == EXIT_SUCCESS
    assert "section,item,metric,value,unit" in out_csv


def test_cli_error_handling(tmp_path: Path) -> None:
    """Verify unknown packs or missing records return clean exit code 1."""
    env = _cli_env(tmp_path)
    code, _, err = _invoke_wt(
        "report generate --start 2026-01-01 --end 2026-01-31 --pack unknown_pack", env
    )
    assert code != EXIT_SUCCESS
    assert "error:" in err


# endregion
