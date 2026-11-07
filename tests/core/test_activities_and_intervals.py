"""Behavioural tests for Activities and structured vs. chaotic Intervals."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from worktrack.core import (
    ACTIVITY_KIND_STANDING,
    ACTIVITY_KIND_TARGETED,
    CAPTURE_MODE_CHAOTIC,
    CAPTURE_MODE_STRUCTURED,
    OVERLAP_POLICY_ALLOW,
    OVERLAP_POLICY_CLOSE_PREVIOUS,
    OVERLAP_POLICY_FORBID,
    ActivityNotFoundError,
    IntervalNotFoundError,
    IntervalSlice,
    IntervalStateError,
    Ledger,
    LedgerError,
    LedgerValidationError,
    NoActiveIntervalError,
    OverlapConflictError,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
BASE_START = datetime(2026, 11, 7, 9, 0, 0, tzinfo=UTC)
THIRTY_MINUTES = timedelta(minutes=30)
FORTY_FIVE_MINUTES = timedelta(minutes=45)
ONE_HOUR = timedelta(hours=1)
NINETY_MINUTES = timedelta(minutes=90)
TWO_HOURS = timedelta(hours=2)
TWO_HOURS_SECONDS = 7200.0
THIRTY_MIN_SECONDS = 1800.0
FORTY_FIVE_MIN_SECONDS = 2700.0
EXPECTED_SPLIT_COUNT = 3
EXPECTED_TWO_ACTIVITIES = 2
EXPECTED_TWO_OPEN = 2
# endregion


# region Fixtures
@pytest.fixture
def telemetry_recorder(tmp_path: Path) -> TelemetryRecorder:
    """Create an isolated TelemetryRecorder for verifying interval counters."""
    return TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")


@pytest.fixture
def ledger(tmp_path: Path, telemetry_recorder: TelemetryRecorder) -> Iterator[Ledger]:
    """Create a migrated file-backed Ledger wired to TelemetryRecorder."""
    instance = Ledger(tmp_path / "ledger.sqlite3", telemetry=telemetry_recorder)
    yield instance
    instance.close()


# endregion


# region Activity Tests
def test_targeted_and_standing_activities_with_tags_and_archival(ledger: Ledger) -> None:
    """Verify Targeted and Standing Activity persistence, tags, and archival guards."""
    targeted = ledger.create_activity(
        "Implement SQLite WAL migrations",
        kind=ACTIVITY_KIND_TARGETED,
        external_ref="PROJ-142",
        source="jira",
        tags=("backend", "sqlite", "backend"),
    )
    standing = ledger.create_activity(
        "Incident Triage",
        kind=ACTIVITY_KIND_STANDING,
        rollup_target_ref="OPS-10",
        tags=("ops", "oncall"),
    )

    assert (targeted.kind, targeted.external_ref) == (ACTIVITY_KIND_TARGETED, "PROJ-142")
    assert targeted.tags == ("backend", "sqlite")
    assert (standing.kind, standing.rollup_target_ref) == (ACTIVITY_KIND_STANDING, "OPS-10")
    assert ledger.find_activity_by_external_ref("PROJ-142", source="jira") == targeted
    assert ledger.list_activities(kind=ACTIVITY_KIND_STANDING) == [standing]
    assert ledger.list_activities(tag="sqlite") == [targeted]

    archived = ledger.update_activity(
        targeted.id, archived=True, tags=("backend", "done"), rollup_target_ref=None
    )
    assert (archived.archived, archived.tags) == (True, ("backend", "done"))
    assert ledger.list_activities() == [standing]
    assert len(ledger.list_activities(include_archived=True)) == EXPECTED_TWO_ACTIVITIES

    with pytest.raises(LedgerValidationError):
        ledger.clock_in(archived.id, started_at=BASE_START)
    with pytest.raises(ActivityNotFoundError):
        ledger.get_activity("missing-activity")


# endregion


# region Interval Tests
def test_structured_clock_in_out_and_overlap_policies(
    ledger: Ledger, telemetry_recorder: TelemetryRecorder
) -> None:
    """Verify structured clock-in/out and forbid/close_previous/allow overlap policies."""
    coding = ledger.create_activity("Feature Development", external_ref="PROJ-200")
    review = ledger.create_activity("Code Review", kind=ACTIVITY_KIND_STANDING)
    with pytest.raises(NoActiveIntervalError):
        ledger.clock_out(ended_at=BASE_START)

    first = ledger.clock_in(coding.id, started_at=BASE_START, notes="Focus", tags=("deep-work",))
    assert (first.is_open, first.capture_mode) == (True, CAPTURE_MODE_STRUCTURED)
    with pytest.raises(OverlapConflictError):
        ledger.clock_in(
            review.id, started_at=BASE_START + THIRTY_MINUTES, overlap_policy=OVERLAP_POLICY_FORBID
        )

    second_start = BASE_START + ONE_HOUR
    second = ledger.clock_in(
        review.id, started_at=second_start, overlap_policy=OVERLAP_POLICY_CLOSE_PREVIOUS
    )
    assert ledger.get_interval(first.id).duration_seconds == ONE_HOUR.total_seconds()
    assert ledger.get_open_intervals() == [second]

    third = ledger.clock_in(
        coding.id, started_at=second_start + THIRTY_MINUTES, overlap_policy=OVERLAP_POLICY_ALLOW
    )
    assert len(ledger.get_open_intervals()) == EXPECTED_TWO_OPEN
    with pytest.raises(OverlapConflictError):
        ledger.clock_out(ended_at=BASE_START + TWO_HOURS)

    closed_second = ledger.clock_out(
        interval_id=second.id, ended_at=BASE_START + NINETY_MINUTES, notes="PR #12"
    )
    closed_third = ledger.clock_out(ended_at=BASE_START + TWO_HOURS)
    assert (closed_second.notes, closed_third.id) == ("PR #12", third.id)
    assert ledger.get_open_intervals() == []
    assert (
        'worktrack_intervals_recorded_total{mode="structured"} 3.0'
        in telemetry_recorder.render_metrics_text()
    )


def test_interval_time_range_queries_and_utc_normalization(ledger: Ledger) -> None:
    """Verify half-open `[since, until)` queries, tag filters, and UTC enforcement."""
    activity = ledger.create_activity("Architecture")
    cet = timezone(timedelta(hours=1))
    naive_ts = datetime(2026, 11, 7, 9, 0, 0)
    with pytest.raises(LedgerValidationError):
        ledger.clock_in(activity.id, started_at=naive_ts)

    first = ledger.record_structured_interval(
        activity.id,
        started_at=datetime(2026, 11, 7, 10, 0, 0, tzinfo=cet),
        ended_at=BASE_START + ONE_HOUR,
        tags=("adr",),
    )
    second = ledger.record_structured_interval(
        activity.id,
        started_at=BASE_START + ONE_HOUR,
        ended_at=BASE_START + TWO_HOURS,
        tags=("spec",),
    )
    assert first.started_at == BASE_START

    window = ledger.list_intervals(since=BASE_START + ONE_HOUR, until=BASE_START + TWO_HOURS)
    assert [item.id for item in window] == [second.id]
    assert [item.id for item in ledger.list_intervals(tag="adr")] == [first.id]


def test_chaotic_capture_and_retroactive_binding(
    ledger: Ledger, telemetry_recorder: TelemetryRecorder
) -> None:
    """Verify chaotic unallocated interval capture and retroactive Activity binding."""
    triage = ledger.create_activity("Incident Triage", kind=ACTIVITY_KIND_STANDING)
    bugfix = ledger.create_activity("Fix Auth Race", external_ref="PROJ-301")

    unallocated = ledger.record_chaotic_interval(
        started_at=BASE_START,
        ended_at=BASE_START + THIRTY_MINUTES,
        notes="Morning Slack & logs",
        tags=("chaotic-am",),
    )
    assert (unallocated.activity_id, unallocated.capture_mode) == (None, CAPTURE_MODE_CHAOTIC)
    assert [i.id for i in ledger.list_intervals(unallocated_only=True)] == [unallocated.id]

    bound = ledger.bind_interval(unallocated.id, triage.id, notes="Pager triage")
    assert (bound.activity_id, bound.notes) == (triage.id, "Pager triage")
    with pytest.raises(IntervalStateError):
        ledger.bind_interval(unallocated.id, bugfix.id)

    rebound = ledger.bind_interval(unallocated.id, bugfix.id, allow_rebind=True)
    assert rebound.activity_id == bugfix.id
    assert (
        'worktrack_intervals_recorded_total{mode="chaotic"} 1.0'
        in telemetry_recorder.render_metrics_text()
    )


def test_chaotic_atomic_split_preserves_duration_and_rolls_back_errors(
    ledger: Ledger,
) -> None:
    """Verify atomic split_interval conserves total duration and rolls back mismatches."""
    bugfix = ledger.create_activity("Fix Auth Race", external_ref="PROJ-301")
    meetings = ledger.create_activity("Standup", kind=ACTIVITY_KIND_STANDING)
    block_start = BASE_START + ONE_HOUR
    chaotic_block = ledger.record_chaotic_interval(
        started_at=block_start,
        ended_at=block_start + TWO_HOURS,
        notes="Afternoon deep dive",
        tags=("afternoon",),
    )
    ledger.assert_context_fact("workplace", "home_office", interval_id=chaotic_block.id)

    with pytest.raises(LedgerValidationError):
        ledger.split_interval(
            chaotic_block.id,
            [
                IntervalSlice(activity_id=bugfix.id, duration=THIRTY_MINUTES),
                IntervalSlice(activity_id=meetings.id, duration=THIRTY_MINUTES),
            ],
        )
    assert ledger.get_interval(chaotic_block.id).duration_seconds == TWO_HOURS_SECONDS

    slices = ledger.split_interval(
        chaotic_block.id,
        [
            IntervalSlice(activity_id=meetings.id, ended_at=block_start + THIRTY_MINUTES),
            IntervalSlice(activity_id=bugfix.id, duration=FORTY_FIVE_MINUTES, notes="Root cause"),
            IntervalSlice(activity_id=None, duration=FORTY_FIVE_MINUTES, notes="Unassigned wrap"),
        ],
    )
    assert len(slices) == EXPECTED_SPLIT_COUNT
    with pytest.raises(IntervalNotFoundError):
        ledger.get_interval(chaotic_block.id)

    assert sum(s.duration_seconds or 0.0 for s in slices) == pytest.approx(TWO_HOURS_SECONDS)
    assert slices[0].duration_seconds == pytest.approx(THIRTY_MIN_SECONDS)
    assert slices[1].duration_seconds == pytest.approx(FORTY_FIVE_MIN_SECONDS)
    assert slices[2].activity_id is None
    assert ledger.get_context_fact("workplace", interval_id=slices[0].id) is not None


def test_tuple_slice_splitting_and_in_memory_closed_lifecycle() -> None:
    """Verify `(activity_id, span, notes)` tuple slices, `:memory:`, and close guard."""
    mem_ledger = Ledger(":memory:", telemetry=None)
    assert mem_ledger.db_path is None
    act_a = mem_ledger.create_activity("Slice A")
    act_b = mem_ledger.create_activity("Slice B")
    raw = mem_ledger.record_chaotic_interval(started_at=BASE_START, ended_at=BASE_START + ONE_HOUR)
    parts = mem_ledger.split_interval(
        raw.id,
        [
            (act_a.id, BASE_START + THIRTY_MINUTES, "First half"),
            (act_b.id, THIRTY_MINUTES),
        ],
    )
    assert [p.activity_id for p in parts] == [act_a.id, act_b.id]
    assert parts[0].notes == "First half"

    mem_ledger.close()
    mem_ledger.close()
    with pytest.raises(LedgerError):
        mem_ledger.list_activities()


# endregion
