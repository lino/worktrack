"""Behavioural tests for `PlausibilityReconciler` and `RetroSplitEngine` (`WT-WP-10`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from worktrack.agenda import AgendaPlanner
from worktrack.core.ledger import Ledger
from worktrack.core.models import BUILTIN_WORKPLACE_FACET_KEY
from worktrack.reconciler import (
    ALLOWED_RETROSPLIT_STRATEGIES,
    GAP_REASON_BELOW_MIN_DURATION,
    GAP_REASON_CLUE_WITHOUT_INTERVAL,
    GAP_REASON_COMPLETED_ACTIVITY,
    RETROSPLIT_STRATEGY_EQUAL,
    RETROSPLIT_STRATEGY_PROPORTIONAL,
    RETROSPLIT_STRATEGY_WEIGHTS,
    PlausibilityReconciler,
    ReconcileClueInput,
    ReconcilerValidationError,
    RetroSplitConflictError,
    RetroSplitEngine,
)
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
WINDOW_START: Final = datetime(2026, 11, 7, 9, 0, 0, tzinfo=UTC)
WINDOW_END: Final = datetime(2026, 11, 7, 17, 0, 0, tzinfo=UTC)
OUTSIDE_BEFORE: Final = datetime(2026, 11, 7, 8, 30, 0, tzinfo=UTC)
OUTSIDE_AFTER: Final = datetime(2026, 11, 7, 18, 0, 0, tzinfo=UTC)

ONE_HOUR_SECONDS: Final = 3600
TWO_HOURS_SECONDS: Final = 7200
THIRTY_MIN_SECONDS: Final = 1800
TEN_MIN_SECONDS: Final = 600
FIVE_MIN_SECONDS: Final = 300
ODD_DURATION_100: Final = 100
ODD_DURATION_101: Final = 101

BUDGET_MINUTES_HIGH: Final = 60
BUDGET_MINUTES_LOW: Final = 30

EXPECTED_PROPORTIONAL_HIGH_SEC: Final = 2700
EXPECTED_PROPORTIONAL_LOW_SEC: Final = 900
EXPECTED_BUDGET_HIGH_SEC: Final = 2400
EXPECTED_BUDGET_LOW_SEC: Final = 1200
EXPECTED_EQUAL_FIRST_100: Final = 34
EXPECTED_EQUAL_OTHER_100: Final = 33
EXPECTED_WEIGHTED_101_A: Final = 20
EXPECTED_WEIGHTED_101_B: Final = 51
EXPECTED_WEIGHTED_101_C: Final = 30
# endregion


# region Fixtures
@dataclass(frozen=True)
class _DuckTypedClue:
    """Duck-typed ambient clue object verifying zero hard coupling to `collectors`."""

    issue_key: str
    occurred_at: datetime
    source: str = "git"
    summary: str = "commit hint"


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    """Return an isolated SQLite database path for reconciler tests."""
    return tmp_path / "reconciler_node.sqlite3"


@pytest.fixture
def telemetry(tmp_path: Path) -> TelemetryRecorder:
    """Return an isolated `TelemetryRecorder` for verifying chaotic interval metrics."""
    return TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")


# endregion


# region Plausibility Detector Tests
def test_reconciler_detects_done_agenda_items_and_clue_gaps_with_candidates(
    db_file: Path,
) -> None:
    """Verify `PlausibilityReconciler` finds unbooked done items, clue gaps, and chaotic blocks."""
    with Ledger(db_file) as ledger, AgendaPlanner(db_file, ledger=ledger) as planner:
        act_done = ledger.create_activity("Fix OAuth Refresh", external_ref="WT-101")
        act_clue = ledger.create_activity("Refactor IPC Framing", external_ref="WT-102")
        act_ok = ledger.create_activity("Write Zensical Docs", external_ref="WT-103")
        act_old = ledger.create_activity("Old Backlog Cleanup", external_ref="WT-104")
        planner.schedule_item(act_done.id, daily_budget_minutes=BUDGET_MINUTES_HIGH, status="done")

        ledger.record_structured_interval(
            act_ok.id,
            started_at=WINDOW_START,
            ended_at=WINDOW_START + timedelta(seconds=THIRTY_MIN_SECONDS),
        )
        chaotic = ledger.record_chaotic_interval(
            started_at=WINDOW_START + timedelta(hours=1),
            ended_at=WINDOW_START + timedelta(hours=2),
        )
        clues = [
            ReconcileClueInput(
                activity_id=act_clue.id, timestamp=WINDOW_START + timedelta(minutes=45)
            ),
            _DuckTypedClue(issue_key="WT-102", occurred_at=WINDOW_START + timedelta(minutes=90)),
            ReconcileClueInput(
                external_ref="WT-103", timestamp=WINDOW_START + timedelta(minutes=15)
            ),
            ReconcileClueInput(activity_id=act_old.id, timestamp=OUTSIDE_BEFORE),
        ]
        report = PlausibilityReconciler(ledger, agenda_planner=planner).inspect(
            WINDOW_START, WINDOW_END, clues=clues
        )

        assert (report.has_gaps, report.has_candidates) == (True, True)
        assert report.total_unallocated_seconds == ONE_HOUR_SECONDS
        assert report.candidate_interval_ids == (chaotic.id,)
        assert set(report.gap_activity_ids) == {act_done.id, act_clue.id}

        gaps_by_id = {gap.activity.id: gap for gap in report.gaps}
        assert gaps_by_id[act_done.id].completed_in_agenda is True
        assert GAP_REASON_COMPLETED_ACTIVITY in gaps_by_id[act_done.id].reasons
        assert gaps_by_id[act_done.id].agenda_budget_seconds == BUDGET_MINUTES_HIGH * 60
        assert gaps_by_id[act_clue.id].clue_count == 2
        assert GAP_REASON_CLUE_WITHOUT_INTERVAL in gaps_by_id[act_clue.id].reasons
        assert report.to_dict()["total_unallocated_seconds"] == ONE_HOUR_SECONDS


def test_reconciler_detects_below_min_booked_seconds_and_validates_bounds(
    db_file: Path,
) -> None:
    """Verify below-threshold booked duration detection and window validation."""
    with Ledger(db_file) as ledger:
        act_short = ledger.create_activity("Short Review", tags=("done",))
        ledger.record_structured_interval(
            act_short.id,
            started_at=WINDOW_START,
            ended_at=WINDOW_START + timedelta(seconds=FIVE_MIN_SECONDS),
        )
        reconciler = PlausibilityReconciler(ledger, min_booked_seconds=TEN_MIN_SECONDS)
        gaps = reconciler.detect_gaps(WINDOW_START, WINDOW_END)

        assert len(gaps) == 1
        assert gaps[0].activity.id == act_short.id
        assert gaps[0].booked_seconds == FIVE_MIN_SECONDS
        assert GAP_REASON_BELOW_MIN_DURATION in gaps[0].reasons

        with pytest.raises(ReconcilerValidationError):
            reconciler.inspect(WINDOW_END, WINDOW_START)
        with pytest.raises(ReconcilerValidationError):
            reconciler.inspect(WINDOW_START, WINDOW_END, min_booked_seconds=-1)


def test_reconciler_supports_mapping_clues_and_chaotic_allocated_candidates(
    db_file: Path,
) -> None:
    """Verify mapping clue coercion, `find_candidate_intervals`, and chaotic allocated flag."""
    with Ledger(db_file) as ledger:
        act_mapped = ledger.create_activity("Mapped Clue Activity", external_ref="WT-301")
        chaotic_bound = ledger.record_chaotic_interval(
            started_at=WINDOW_START,
            ended_at=WINDOW_START + timedelta(seconds=THIRTY_MIN_SECONDS),
            activity_id=act_mapped.id,
        )
        reconciler = PlausibilityReconciler(ledger, min_booked_seconds=ONE_HOUR_SECONDS)
        assert reconciler.min_booked_seconds == ONE_HOUR_SECONDS
        assert reconciler.find_candidate_intervals(WINDOW_START, WINDOW_END) == []
        candidates = reconciler.find_candidate_intervals(
            WINDOW_START, WINDOW_END, include_chaotic_allocated=True
        )
        assert [item.id for item in candidates] == [chaotic_bound.id]

        gaps = reconciler.detect_gaps(
            WINDOW_START,
            WINDOW_END,
            clues=[
                {
                    "external_ref": "WT-301",
                    "timestamp": "2026-11-07T10:00:00Z",
                    "source": "calendar",
                    "summary": "Sprint sync",
                    "weight": 2.0,
                }
            ],
        )
        assert len(gaps) == 1
        assert (gaps[0].activity_id, gaps[0].external_ref) == (act_mapped.id, "WT-301")
        assert gaps[0].suggested_weight == 2.0


# endregion


# region RetroSplit Proposal Tests
def test_retrosplit_proportional_by_clue_count_and_agenda_budget(db_file: Path) -> None:
    """Verify `RETROSPLIT_STRATEGY_PROPORTIONAL` weights by clues, budgets, or weight hints."""
    assert {
        RETROSPLIT_STRATEGY_PROPORTIONAL,
        RETROSPLIT_STRATEGY_EQUAL,
        RETROSPLIT_STRATEGY_WEIGHTS,
    } == ALLOWED_RETROSPLIT_STRATEGIES

    with Ledger(db_file) as ledger, AgendaPlanner(db_file, ledger=ledger) as planner:
        act_a = ledger.create_activity("Feature Alpha", external_ref="WT-201")
        act_b = ledger.create_activity("Feature Beta", external_ref="WT-202")
        candidate = ledger.record_chaotic_interval(
            started_at=WINDOW_START,
            ended_at=WINDOW_START + timedelta(seconds=ONE_HOUR_SECONDS),
        )

        clues = [
            ReconcileClueInput(activity_id=act_a.id),
            ReconcileClueInput(activity_id=act_a.id),
            ReconcileClueInput(activity_id=act_a.id),
            ReconcileClueInput(activity_id=act_b.id),
        ]
        engine = RetroSplitEngine(ledger, agenda_planner=planner)
        clue_proposal = engine.propose_split(
            candidate,
            strategy=RETROSPLIT_STRATEGY_PROPORTIONAL,
            activity_ids=[act_a.id, act_b.id],
            clues=clues,
        )
        assert clue_proposal.is_conserved is True
        assert clue_proposal.slice_durations == (
            EXPECTED_PROPORTIONAL_HIGH_SEC,
            EXPECTED_PROPORTIONAL_LOW_SEC,
        )

        planner.schedule_item(act_a.id, daily_budget_minutes=BUDGET_MINUTES_HIGH, status="done")
        planner.schedule_item(act_b.id, daily_budget_minutes=BUDGET_MINUTES_LOW, status="done")
        budget_proposal = engine.propose_split(
            candidate,
            strategy=RETROSPLIT_STRATEGY_PROPORTIONAL,
            activity_ids=[act_a.id, act_b.id],
        )
        assert budget_proposal.is_conserved is True
        assert budget_proposal.slice_durations == (
            EXPECTED_BUDGET_HIGH_SEC,
            EXPECTED_BUDGET_LOW_SEC,
        )


def test_retrosplit_equal_and_weights_conserve_every_second_deterministically(
    db_file: Path,
) -> None:
    """Verify `equal` and `weights` strategies assign rounding remainders to highest weight."""
    with Ledger(db_file) as ledger:
        act_a = ledger.create_activity("Slice A")
        act_b = ledger.create_activity("Slice B")
        act_c = ledger.create_activity("Slice C")

        interval_100 = ledger.record_chaotic_interval(
            started_at=WINDOW_START,
            ended_at=WINDOW_START + timedelta(seconds=ODD_DURATION_100),
        )
        interval_101 = ledger.record_chaotic_interval(
            started_at=WINDOW_START + timedelta(minutes=10),
            ended_at=WINDOW_START + timedelta(minutes=10, seconds=ODD_DURATION_101),
        )

        engine = RetroSplitEngine(ledger)
        equal_prop = engine.propose_split(
            interval_100,
            strategy=RETROSPLIT_STRATEGY_EQUAL,
            activity_ids=[act_a.id, act_b.id, act_c.id],
        )
        assert sum(equal_prop.slice_durations) == ODD_DURATION_100
        assert equal_prop.slice_durations == (
            EXPECTED_EQUAL_FIRST_100,
            EXPECTED_EQUAL_OTHER_100,
            EXPECTED_EQUAL_OTHER_100,
        )

        weighted_prop = engine.propose_split(
            interval_101,
            strategy=RETROSPLIT_STRATEGY_WEIGHTS,
            weights={act_a.id: 0.2, act_b.id: 0.5, act_c.id: 0.3},
        )
        assert sum(weighted_prop.slice_durations) == ODD_DURATION_101
        assert weighted_prop.slice_durations == (
            EXPECTED_WEIGHTED_101_A,
            EXPECTED_WEIGHTED_101_B,
            EXPECTED_WEIGHTED_101_C,
        )
        assert weighted_prop.to_dict()["is_conserved"] is True

        with pytest.raises(ReconcilerValidationError):
            engine.propose_split(interval_100, strategy="invalid", activity_ids=[act_a.id])
        with pytest.raises(ReconcilerValidationError):
            engine.propose_split(
                interval_100,
                strategy=RETROSPLIT_STRATEGY_WEIGHTS,
                weights={act_a.id: -0.5, act_b.id: 1.0},
            )


# endregion


# region RetroSplit Application Tests
def test_apply_proposal_binds_single_slice_and_splits_multi_slice_with_telemetry(
    db_file: Path,
    telemetry: TelemetryRecorder,
) -> None:
    """Verify `apply_proposal` binds 1-slice, splits >=2 slices, and increments chaotic metric."""
    with Ledger(db_file, telemetry=telemetry) as ledger:
        act_one = ledger.create_activity("Single Target")
        act_two = ledger.create_activity("Second Target")

        single_block = ledger.record_chaotic_interval(
            started_at=WINDOW_START,
            ended_at=WINDOW_START + timedelta(seconds=THIRTY_MIN_SECONDS),
        )
        multi_block = ledger.record_chaotic_interval(
            started_at=WINDOW_START + timedelta(hours=1),
            ended_at=WINDOW_START + timedelta(hours=2),
            tags=("retro",),
        )
        ledger.assert_context_fact(
            BUILTIN_WORKPLACE_FACET_KEY, "home_office", interval_id=multi_block.id
        )

        engine = RetroSplitEngine(ledger)
        single_prop = engine.propose_split(
            single_block, strategy=RETROSPLIT_STRATEGY_EQUAL, activity_ids=[act_one.id]
        )
        single_result = engine.apply_proposal(single_prop)
        assert len(single_result) == 1
        assert (single_result[0].id, single_result[0].activity_id) == (single_block.id, act_one.id)
        assert single_result[0].duration_seconds == THIRTY_MIN_SECONDS

        multi_prop = engine.propose_split(
            multi_block,
            strategy=RETROSPLIT_STRATEGY_WEIGHTS,
            weights={act_one.id: 0.25, act_two.id: 0.75},
        )
        multi_result = engine.apply_proposal(multi_prop)
        assert len(multi_result) == 2
        assert multi_result[0].duration_seconds == EXPECTED_PROPORTIONAL_LOW_SEC
        assert multi_result[1].duration_seconds == EXPECTED_PROPORTIONAL_HIGH_SEC
        assert multi_result[0].ended_at == multi_result[1].started_at
        assert round(sum(item.duration_seconds or 0.0 for item in multi_result)) == ONE_HOUR_SECONDS

        for child in multi_result:
            fact = ledger.get_context_fact(BUILTIN_WORKPLACE_FACET_KEY, interval_id=child.id)
            assert fact is not None and fact.value == "home_office"

        with pytest.raises(RetroSplitConflictError):
            engine.apply_proposal(multi_prop)

        metrics_text = telemetry.render_metrics_text()
        assert 'worktrack_intervals_recorded_total{mode="chaotic"} 5.0' in metrics_text


def test_propose_from_report_and_apply_across_multiple_candidate_intervals(
    db_file: Path,
) -> None:
    """Verify end-to-end `inspect -> propose_from_report -> apply_proposal` across blocks."""
    with Ledger(db_file) as ledger, AgendaPlanner(db_file, ledger=ledger) as planner:
        act_a = ledger.create_activity("Task A")
        act_b = ledger.create_activity("Task B")
        planner.schedule_item(act_a.id, daily_budget_minutes=BUDGET_MINUTES_HIGH, status="done")
        planner.schedule_item(act_b.id, daily_budget_minutes=BUDGET_MINUTES_LOW, status="done")

        ledger.record_chaotic_interval(
            started_at=WINDOW_START,
            ended_at=WINDOW_START + timedelta(seconds=THIRTY_MIN_SECONDS),
        )
        ledger.record_chaotic_interval(
            started_at=WINDOW_START + timedelta(hours=2),
            ended_at=WINDOW_START + timedelta(hours=2, seconds=THIRTY_MIN_SECONDS),
        )

        reconciler = PlausibilityReconciler(ledger, agenda_planner=planner)
        report = reconciler.inspect(WINDOW_START, WINDOW_END)
        engine = RetroSplitEngine(ledger, agenda_planner=planner)
        proposal = engine.propose_from_report(report, strategy=RETROSPLIT_STRATEGY_PROPORTIONAL)

        assert proposal.total_duration_seconds == TWO_HOURS_SECONDS // 2
        applied = engine.apply_proposal(proposal)
        assert len(applied) == 4 and applied.total_duration_seconds == ONE_HOUR_SECONDS

        post_report = reconciler.inspect(WINDOW_START, WINDOW_END)
        assert post_report.has_gaps is False and post_report.has_candidates is False


def test_apply_proposal_handles_archived_activities_and_open_interval_conflicts(
    db_file: Path,
) -> None:
    """Verify `apply_proposal` preserves archived flag and rejects open intervals."""
    with Ledger(db_file) as ledger:
        archived_act = ledger.create_activity("Closed Ticket", archived=True)
        active_act = ledger.create_activity("Open Ticket")
        chaotic_block = ledger.record_chaotic_interval(
            started_at=WINDOW_START,
            ended_at=WINDOW_START + timedelta(seconds=ONE_HOUR_SECONDS),
        )
        open_timer = ledger.clock_in(active_act.id, started_at=OUTSIDE_AFTER)

        engine = RetroSplitEngine(ledger)
        with pytest.raises(ReconcilerValidationError):
            engine.propose_split(open_timer, activity_ids=[active_act.id])

        proposal = engine.propose_split(
            chaotic_block.id,
            strategy=RETROSPLIT_STRATEGY_PROPORTIONAL,
            weights={archived_act.id: 3.0, active_act.id: 1.0},
            notes_by_activity={archived_act.id: "Retroactive fix"},
        )
        assert (proposal.interval_id, proposal.original_duration_seconds) == (
            chaotic_block.id,
            ONE_HOUR_SECONDS,
        )

        result = engine.apply_proposal(proposal)
        assert len(result) == 2 and result[0].notes == "Retroactive fix"
        assert next(iter(result)).activity_id == archived_act.id
        assert result.to_dict()["interval_count"] == 2
        assert ledger.get_activity(archived_act.id).archived is True


# endregion
