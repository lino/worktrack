"""Behavioural tests for `worktrack.agenda` Horizon Planner, `FocusBlocks`, and insights."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from worktrack.agenda import (
    AGENDA_SCHEMA_VERSION_INITIAL,
    AGENDA_STATUS_DEFERRED,
    AGENDA_STATUS_DONE,
    AGENDA_STATUS_IN_PROGRESS,
    AGENDA_STATUS_PLANNED,
    DEFAULT_FOCUS_LABEL,
    HORIZON_BACKLOG,
    HORIZON_THIS_WEEK,
    HORIZON_TODAY,
    AgendaItemNotFoundError,
    AgendaPlanner,
    AgendaPriority,
    AgendaValidationError,
    FocusBlock,
    FocusBlockNotFoundError,
)
from worktrack.core import (
    ACTIVITY_KIND_STANDING,
    ACTIVITY_KIND_TARGETED,
    BUILTIN_WORKPLACE_FACET_KEY,
    ENTITY_KIND_PROJECT,
    SCHEMA_VERSION_INITIAL,
    Activity,
    Ledger,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
PRIORITY_TOP = 1
PRIORITY_SECOND = 2
PRIORITY_LOW = 4
BUDGET_MINUTES_NINETY = 90
BUDGET_SECONDS_NINETY_MIN = 5_400
BUDGET_SECONDS_SIXTY_MIN = 3_600
TRACKED_THIRTY_MIN_SECONDS = 1_800
ACTIVE_TWENTY_MIN_SECONDS = 1_200
WEEK_PREVIOUS_DAY_SECONDS = 7_200
CHAOTIC_FIFTEEN_MIN_SECONDS = 900
WEIGHT_ALPHA = 0.75
WEIGHT_BETA = 0.25
EXPECTED_SCHEDULED_THREE = 3.0
EXPECTED_HOME_OFFICE_DAYS = 2
EXPECTED_OFFICE_DAYS = 1
EXPECTED_ZERO_DAYS = 0
# endregion


# region Fixtures
@pytest.fixture
def agenda_workspace(
    tmp_path: Path,
) -> Iterator[tuple[Path, Ledger, AgendaPlanner, TelemetryRecorder]]:
    """Yield shared SQLite ledger, agenda planner, and telemetry recorder, closing on teardown."""
    db_path = tmp_path / "data" / "ledger.sqlite3"
    recorder = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    with (
        Ledger(db_path, telemetry=recorder) as ledger,
        AgendaPlanner(db_path, ledger=ledger, telemetry=recorder) as planner,
    ):
        yield db_path, ledger, planner, recorder


def _seed_horizon_dataset(
    ledger: Ledger,
    planner: AgendaPlanner,
) -> tuple[Activity, FocusBlock, FocusBlock]:
    """Populate activities, attributions, intervals, facts, and focus blocks."""
    act_rfc, act_rev = _seed_horizon_ledger(ledger)
    active_block, upcoming_block = _seed_horizon_planner(planner, act_rfc, act_rev)
    return act_rfc, active_block, upcoming_block


def _seed_horizon_ledger(ledger: Ledger) -> tuple[Activity, Activity]:
    """Populate entities, attributions, intervals, and workplace facts in `ledger`."""
    proj_alpha = ledger.create_entity(ENTITY_KIND_PROJECT, "alpha", "Project Alpha")
    proj_beta = ledger.create_entity(ENTITY_KIND_PROJECT, "beta", "Project Beta")
    act_rfc = ledger.create_activity(
        "Auth RFC", kind=ACTIVITY_KIND_TARGETED, external_ref="PROJ-201"
    )
    act_rev = ledger.create_activity("Code Review", kind=ACTIVITY_KIND_STANDING)
    ledger.set_attribution(
        source_activity_id=act_rfc.id, target_entity_id=proj_alpha.id, weight=WEIGHT_ALPHA
    )
    ledger.set_attribution(
        source_activity_id=act_rfc.id, target_entity_id=proj_beta.id, weight=WEIGHT_BETA
    )
    ledger.record_structured_interval(
        act_rev.id,
        started_at=datetime(2026, 11, 6, 14, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 6, 16, 0, tzinfo=UTC),
    )
    ledger.assert_context_fact(BUILTIN_WORKPLACE_FACET_KEY, "office", scope_date="2026-11-06")
    ledger.assert_context_fact(BUILTIN_WORKPLACE_FACET_KEY, "home_office", scope_date="2026-11-05")
    ledger.record_structured_interval(
        act_rfc.id,
        started_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 7, 9, 30, tzinfo=UTC),
    )
    ledger.record_chaotic_interval(
        started_at=datetime(2026, 11, 7, 9, 45, tzinfo=UTC),
        ended_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
    )
    ledger.clock_in(act_rfc.id, started_at=datetime(2026, 11, 7, 10, 10, tzinfo=UTC))
    ledger.assert_context_fact(BUILTIN_WORKPLACE_FACET_KEY, "home_office", scope_date="2026-11-07")
    return act_rfc, act_rev


def _seed_horizon_planner(
    planner: AgendaPlanner,
    act_rfc: Activity,
    act_rev: Activity,
) -> tuple[FocusBlock, FocusBlock]:
    """Schedule horizon `AgendaItems` and `FocusBlocks` in `planner`."""
    planner.schedule_item(
        act_rfc.id,
        horizon=HORIZON_TODAY,
        priority=PRIORITY_TOP,
        budget_seconds=BUDGET_SECONDS_NINETY_MIN,
        deadline="2026-11-08T12:00:00Z",
    )
    planner.schedule_item(
        act_rev.id,
        horizon=HORIZON_THIS_WEEK,
        priority=PRIORITY_SECOND,
        budget_seconds=WEEK_PREVIOUS_DAY_SECONDS,
    )
    active_block = planner.schedule_focus_block(
        started_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
        activity_id=act_rfc.id,
        label="Morning Deep Block",
    )
    upcoming_block = planner.schedule_focus_block(
        started_at=datetime(2026, 11, 7, 14, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 7, 15, 30, tzinfo=UTC),
        label="Afternoon Writing",
    )
    return active_block, upcoming_block


# endregion


# region Agenda Schema Tests
def test_agenda_schema_migrations_coexist_independently_with_core_ledger(tmp_path: Path) -> None:
    """Verify `agenda_schema_migrations` does not collide with `schema_migrations`."""
    db_path = tmp_path / "coexist.sqlite3"
    with AgendaPlanner(db_path) as planner, Ledger(db_path) as ledger:
        assert ledger.schema_version == SCHEMA_VERSION_INITIAL
        assert planner.agenda_schema_version == AGENDA_SCHEMA_VERSION_INITIAL
        assert len(ledger.applied_migrations()) == SCHEMA_VERSION_INITIAL
        assert len(planner.applied_agenda_migrations()) == AGENDA_SCHEMA_VERSION_INITIAL

    with closing(sqlite3.connect(db_path)) as raw_conn:
        tables = {
            str(row[0])
            for row in raw_conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "schema_migrations",
        "agenda_schema_migrations",
        "agenda_items",
        "focus_blocks",
    } <= tables


# endregion


# region Agenda & Focus Tests
def test_schedule_agenda_items_and_focus_blocks_increments_telemetry(
    agenda_workspace: tuple[Path, Ledger, AgendaPlanner, TelemetryRecorder],
) -> None:
    """Verify scheduling `AgendaItems` and `FocusBlocks` increments scheduled counter."""
    _, ledger, planner, recorder = agenda_workspace
    rfc_act = ledger.create_activity("Write Auth RFC", external_ref="PROJ-101")
    ops_act = ledger.create_activity("Ops Triage", kind=ACTIVITY_KIND_STANDING)

    item_today = planner.schedule_item(
        rfc_act.id,
        horizon=HORIZON_TODAY,
        priority=AgendaPriority.URGENT,
        daily_budget_minutes=BUDGET_MINUTES_NINETY,
        deadline=date(2026, 11, 8),
        notes="Draft threat model",
    )
    planner.schedule_item(
        ops_act.id,
        horizon=HORIZON_THIS_WEEK,
        priority=PRIORITY_LOW,
        budget_seconds=BUDGET_SECONDS_SIXTY_MIN,
    )
    block = planner.schedule_focus_block(
        started_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 7, 11, 30, tzinfo=UTC),
        activity_id=rfc_act.id,
        label="Deep RFC Focus",
        expose_busy=True,
    )

    assert item_today.budget_seconds == BUDGET_SECONDS_NINETY_MIN
    assert item_today.daily_budget_minutes == BUDGET_MINUTES_NINETY
    assert item_today.deadline == "2026-11-08"
    assert item_today.status == AGENDA_STATUS_PLANNED
    assert (block.expose_busy, block.duration_seconds) == (True, BUDGET_SECONDS_NINETY_MIN)
    metrics_text = recorder.render_metrics_text()
    assert f"worktrack_agenda_items_scheduled_total {EXPECTED_SCHEDULED_THREE}" in metrics_text


def test_update_complete_defer_and_delete_agenda_records(
    agenda_workspace: tuple[Path, Ledger, AgendaPlanner, TelemetryRecorder],
) -> None:
    """Verify `AgendaItem` transitions, `FocusBlock` deletion, and error guards."""
    _, ledger, planner, _ = agenda_workspace
    rfc_act = ledger.create_activity("Write Auth RFC")
    item_today = planner.schedule_item(rfc_act.id, horizon=HORIZON_TODAY)
    item_week = planner.schedule_item(rfc_act.id, horizon=HORIZON_THIS_WEEK)
    block = planner.schedule_focus_block(
        started_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
        label="Focus",
    )

    progressed = planner.update_item(
        item_today.id, status=AGENDA_STATUS_IN_PROGRESS, priority=PRIORITY_SECOND
    )
    assert (progressed.status, progressed.priority) == (AGENDA_STATUS_IN_PROGRESS, PRIORITY_SECOND)

    completed = planner.complete_item(item_today.id)
    assert completed.status == AGENDA_STATUS_DONE
    deferred = planner.update_item(
        item_week.id, horizon=HORIZON_BACKLOG, status=AGENDA_STATUS_DEFERRED
    )
    assert (deferred.horizon, deferred.status) == (HORIZON_BACKLOG, AGENDA_STATUS_DEFERRED)
    assert planner.list_items(horizon=HORIZON_TODAY, include_done=False) == []
    assert [e.id for e in planner.list_items(horizon=HORIZON_TODAY, include_done=True)] == [
        item_today.id
    ]

    planner.delete_focus_block(block.id)
    with pytest.raises(FocusBlockNotFoundError):
        planner.get_focus_block(block.id)
    with pytest.raises(AgendaItemNotFoundError):
        planner.get_item("missing-item-id")
    with pytest.raises(AgendaValidationError):
        planner.schedule_item(rfc_act.id, horizon="decade")


def test_schedule_focus_block_default_label_and_blank_validation(
    agenda_workspace: tuple[Path, Ledger, AgendaPlanner, TelemetryRecorder],
) -> None:
    """Verify `schedule_focus_block` defaults `label` and rejects blank `label`/`block_id`."""
    _, _, planner, _ = agenda_workspace
    start_dt = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    end_dt = datetime(2026, 11, 7, 13, 0, tzinfo=UTC)

    default_block = planner.schedule_focus_block(started_at=start_dt, ended_at=end_dt)
    assert default_block.activity_id is None
    assert default_block.label == DEFAULT_FOCUS_LABEL

    with pytest.raises(AgendaValidationError):
        planner.schedule_focus_block(started_at=start_dt, ended_at=end_dt, label="   ")
    with pytest.raises(AgendaValidationError):
        planner.schedule_focus_block(started_at=start_dt, ended_at=end_dt, block_id="   ")


# endregion


# region Horizon Insight Tests
def test_now_and_today_insights_compute_timers_budgets_deadlines_and_attributions(
    agenda_workspace: tuple[Path, Ledger, AgendaPlanner, TelemetryRecorder],
) -> None:
    """Verify `get_now_insight` and `get_today_insight` summarize timers and attributions."""
    _, ledger, planner, _ = agenda_workspace
    act_rfc, active_block, upcoming_block = _seed_horizon_dataset(ledger, planner)
    ref_now = datetime(2026, 11, 7, 10, 30, tzinfo=UTC)
    expected_today = (
        TRACKED_THIRTY_MIN_SECONDS + CHAOTIC_FIFTEEN_MIN_SECONDS + ACTIVE_TWENTY_MIN_SECONDS
    )

    now_insight = planner.get_now_insight(now=ref_now)
    assert now_insight.active_activity is not None
    assert now_insight.active_activity.id == act_rfc.id
    assert now_insight.active_elapsed_seconds == ACTIVE_TWENTY_MIN_SECONDS
    assert (now_insight.active_focus_block, now_insight.upcoming_focus_block) == (
        active_block,
        upcoming_block,
    )
    assert (now_insight.workplace, now_insight.today_total_seconds) == (
        "home_office",
        expected_today,
    )

    today_insight = planner.get_today_insight(now=ref_now)
    assert today_insight.total_tracked_seconds == expected_today
    assert today_insight.unallocated_seconds == CHAOTIC_FIFTEEN_MIN_SECONDS
    rfc_entry = today_insight.agenda_queue[0]
    expected_rfc_today = TRACKED_THIRTY_MIN_SECONDS + ACTIVE_TWENTY_MIN_SECONDS
    assert rfc_entry.tracked_seconds == expected_rfc_today
    assert rfc_entry.remaining_budget_seconds == BUDGET_SECONDS_NINETY_MIN - expected_rfc_today
    assert rfc_entry.deadline_approaching is True
    assert [share.entity.key for share in today_insight.entity_attributions] == ["alpha", "beta"]


def test_week_insight_and_ipc_today_snapshot_aggregate_workplaces_and_queue(
    agenda_workspace: tuple[Path, Ledger, AgendaPlanner, TelemetryRecorder],
) -> None:
    """Verify `get_week_insight` workplace breakdown and `to_ipc_today_snapshot`."""
    _, ledger, planner, _ = agenda_workspace
    _seed_horizon_dataset(ledger, planner)
    ref_now = datetime(2026, 11, 7, 10, 30, tzinfo=UTC)
    expected_today = (
        TRACKED_THIRTY_MIN_SECONDS + CHAOTIC_FIFTEEN_MIN_SECONDS + ACTIVE_TWENTY_MIN_SECONDS
    )

    week_insight = planner.get_week_insight(now=ref_now)
    assert week_insight.total_tracked_seconds == expected_today + WEEK_PREVIOUS_DAY_SECONDS
    assert week_insight.workplace_breakdown["home_office"] == EXPECTED_HOME_OFFICE_DAYS
    assert week_insight.workplace_breakdown["office"] == EXPECTED_OFFICE_DAYS
    assert week_insight.workplace_breakdown["mobile_work"] == EXPECTED_ZERO_DAYS
    assert len(week_insight.agenda_queue) == 2

    ipc_snapshot = planner.to_ipc_today_snapshot(now=ref_now)
    assert ipc_snapshot.date_iso == "2026-11-07"
    assert ipc_snapshot.logged_total_seconds == expected_today
    assert ipc_snapshot.planned_budget_seconds == BUDGET_SECONDS_NINETY_MIN
    assert len(ipc_snapshot.focus_blocks) == 2


# endregion
