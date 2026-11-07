"""Behavioural tests for `RollupPolicy`, note redaction, rounding, and `PreviewDiff`."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worktrack.core import ACTIVITY_KIND_STANDING, ACTIVITY_KIND_TARGETED, Ledger
from worktrack.outbox import (
    CHANGE_KIND_CREATE,
    CHANGE_KIND_UNCHANGED,
    CHANGE_KIND_UPDATE,
    FLUSH_CADENCE_EOD,
    FLUSH_CADENCE_EOW,
    FLUSH_TRIGGER_MANUAL,
    GROUPING_WINDOW_DAY,
    GROUPING_WINDOW_INTERVAL,
    GROUPING_WINDOW_MONTH,
    GROUPING_WINDOW_WEEK,
    OUTBOX_STATUS_STAGED,
    QUANTUM_FIFTEEN_MINUTES,
    QUANTUM_FIVE_MINUTES,
    QUANTUM_ONE_MINUTE,
    ROUNDING_MODE_CEIL,
    ROUNDING_MODE_FLOOR,
    ROUNDING_MODE_NEAREST,
    ApprovedOutboxGrant,
    OutboxDispatcher,
    OutboxItem,
    OutboxStore,
    OutboxValidationError,
    RollupEngine,
    RollupPolicy,
    SinkWriteReceipt,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
EPOCH_AUG_03 = datetime(2026, 8, 3, 9, 0, 0, tzinfo=UTC)
EPOCH_AUG_04 = datetime(2026, 8, 4, 10, 0, 0, tzinfo=UTC)
EPOCH_AUG_12 = datetime(2026, 8, 12, 14, 0, 0, tzinfo=UTC)
EPOCH_SEP_02 = datetime(2026, 9, 2, 11, 0, 0, tzinfo=UTC)

SHORT_NOISE_SECONDS = 25
MIN_DURATION_THRESHOLD = 60
TWENTY_TWO_MINUTES_SECONDS = 1_320
TWENTY_MINUTES_SECONDS = 1_200
TWENTY_FIVE_MINUTES_SECONDS = 1_500
THIRTY_MINUTES_SECONDS = 1_800
FORTY_FIVE_MINUTES_SECONDS = 2_700
ONE_HOUR_SECONDS = 3_600
NINETY_MINUTES_SECONDS = 5_400
HTTP_CREATED = 201
EXPECTED_TWO_BUCKETS = 2
EXPECTED_THREE_BUCKETS = 3
EXPECTED_FOUR_BUCKETS = 4
SECRET_NOTE_TEXT = "Venting privately about architecture review friction"
# endregion


# region Fixtures
class _RecordingMemorySink:
    """In-memory `RollupSink` recording approved writes for preview diff tests."""

    sink_name = "jira"

    def __init__(self, store: OutboxStore) -> None:
        """Initialize memory sink bound to `store` for grant verification."""
        self._store = store
        self.received: list[OutboxItem] = []

    def write_rollup(
        self,
        item: OutboxItem,
        grant: ApprovedOutboxGrant | None = None,
        *,
        source: str = "direct_call",
    ) -> SinkWriteReceipt:
        """Verify grant and record outbound item."""
        self._store.verify_grant(item, grant, expected_sink=self.sink_name, source=source)
        self.received.append(item)
        return SinkWriteReceipt(
            destination_uri=f"https://api.atlassian.com/ex/jira/cloud/issue/{item.target_ref}/worklog",
            http_status=HTTP_CREATED,
            external_receipt_id=f"wl-{len(self.received)}",
        )


def _seed_anti_surveillance_corpus(ledger: Ledger) -> None:
    """Seed targeted, standing, unmapped, and open intervals with sensitive notes."""
    targeted = ledger.create_activity(
        "Implement OAuth Broker", kind=ACTIVITY_KIND_TARGETED, external_ref="PROJ-142"
    )
    standing_mapped = ledger.create_activity("Meetings", kind=ACTIVITY_KIND_STANDING)
    standing_direct = ledger.create_activity(
        "On-Call Triage", kind=ACTIVITY_KIND_STANDING, rollup_target_ref="OPS-99"
    )
    local_unmapped = ledger.create_activity("Personal Reading", kind=ACTIVITY_KIND_STANDING)

    ledger.record_structured_interval(
        targeted.id,
        started_at=EPOCH_AUG_03,
        ended_at=EPOCH_AUG_03 + timedelta(seconds=TWENTY_MINUTES_SECONDS),
        notes=SECRET_NOTE_TEXT,
        tags=("deep-work",),
    )
    ledger.record_chaotic_interval(
        activity_id=targeted.id,
        started_at=EPOCH_AUG_03 + timedelta(hours=2),
        ended_at=EPOCH_AUG_03 + timedelta(hours=2, seconds=TWENTY_FIVE_MINUTES_SECONDS),
        notes="Second private chaotic note",
    )
    ledger.record_structured_interval(
        standing_mapped.id,
        started_at=EPOCH_AUG_03 + timedelta(hours=4),
        ended_at=EPOCH_AUG_03 + timedelta(hours=4, seconds=THIRTY_MINUTES_SECONDS),
        notes="1:1 career chat",
    )
    ledger.record_structured_interval(
        standing_direct.id,
        started_at=EPOCH_AUG_03 + timedelta(hours=5),
        ended_at=EPOCH_AUG_03 + timedelta(hours=5, seconds=TWENTY_MINUTES_SECONDS),
    )
    ledger.record_structured_interval(
        local_unmapped.id,
        started_at=EPOCH_AUG_03 + timedelta(hours=6),
        ended_at=EPOCH_AUG_03 + timedelta(hours=6, seconds=THIRTY_MINUTES_SECONDS),
        notes="Never export local reading",
    )
    ledger.clock_in(
        targeted.id,
        started_at=EPOCH_AUG_03 + timedelta(hours=7),
        notes="Still open timer must be ignored",
    )


def _seed_window_intervals(ledger: Ledger) -> None:
    """Seed five closed intervals spanning days, weeks, and months."""
    act = ledger.create_activity("Core Feature", external_ref="PROJ-200")
    spans = (
        (EPOCH_AUG_03, SHORT_NOISE_SECONDS),
        (EPOCH_AUG_03 + timedelta(hours=1), TWENTY_TWO_MINUTES_SECONDS),
        (EPOCH_AUG_04, TWENTY_TWO_MINUTES_SECONDS),
        (EPOCH_AUG_12, THIRTY_MINUTES_SECONDS),
        (EPOCH_SEP_02, THIRTY_MINUTES_SECONDS),
    )
    for start, secs in spans:
        ledger.record_structured_interval(
            act.id, started_at=start, ended_at=start + timedelta(seconds=secs)
        )


# endregion


# region Rollup & Preview Tests
def test_rollup_enforces_anti_surveillance_redaction_and_standing_mappings(
    tmp_path: Path,
) -> None:
    """Verify raw notes, Intervals, and Clues never leak into previews unless opted in."""
    db_path = tmp_path / "ledger.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")

    with Ledger(db_path) as ledger, OutboxStore(db_path) as store:
        _seed_anti_surveillance_corpus(ledger)
        policy = RollupPolicy(
            grouping_window=GROUPING_WINDOW_DAY,
            min_duration_seconds=MIN_DURATION_THRESHOLD,
            rounding_quantum_seconds=QUANTUM_FIFTEEN_MINUTES,
            rounding_mode=ROUNDING_MODE_NEAREST,
            redact_notes=True,
            standing_activity_mappings={"Meetings": "OPS-10"},
            flush_cadence=FLUSH_CADENCE_EOD,
        )
        engine = RollupEngine(ledger=ledger, outbox_store=store, telemetry=telemetry)
        preview = engine.preview_rollup(sink="jira", policy=policy)

        assert len(preview.proposals) == EXPECTED_THREE_BUCKETS
        assert preview.unmapped_interval_count == 1
        serialized_preview = json.dumps(preview.to_dict())
        for forbidden in (
            SECRET_NOTE_TEXT,
            "1:1 career chat",
            "Second private chaotic note",
            "Still open timer",
        ):
            assert forbidden not in serialized_preview
        assert "source_interval_ids" not in json.dumps(
            [prop.payload_preview for prop in preview.proposals]
        )

        by_ref = {prop.target_ref: prop for prop in preview.proposals}
        assert set(by_ref) == {"PROJ-142", "OPS-10", "OPS-99"}
        assert by_ref["PROJ-142"].rounded_duration_seconds == FORTY_FIVE_MINUTES_SECONDS
        assert by_ref["OPS-10"].rounded_duration_seconds == THIRTY_MINUTES_SECONDS
        assert by_ref["OPS-99"].rounded_duration_seconds == QUANTUM_FIFTEEN_MINUTES
        assert by_ref["PROJ-142"].summary == "Implement OAuth Broker"

    metrics_text = telemetry.render_metrics_text()
    assert (
        'worktrack_rollup_compute_duration_seconds_count{cadence="eod",outcome="ok"} 1.0'
        in metrics_text
    )


def test_rollup_explicit_note_opt_in_and_template_guard(tmp_path: Path) -> None:
    """Verify `redact_notes=False` includes notes while `redact_notes=True` blocks `{notes}`."""
    with pytest.raises(OutboxValidationError):
        RollupPolicy(redact_notes=True, summary_template="{activity_name}: {notes}")

    db_path = tmp_path / "optin.sqlite3"
    with Ledger(db_path) as ledger, OutboxStore(db_path) as store:
        act = ledger.create_activity("Pair Debugging", external_ref="PROJ-77")
        ledger.record_structured_interval(
            act.id,
            started_at=EPOCH_AUG_03,
            ended_at=EPOCH_AUG_03 + timedelta(seconds=THIRTY_MINUTES_SECONDS),
            notes="Investigated connection pool timeout",
        )
        policy = RollupPolicy(redact_notes=False, rounding_quantum_seconds=QUANTUM_ONE_MINUTE)
        preview = RollupEngine(ledger=ledger, outbox_store=store).preview_rollup(
            sink="jira", policy=policy
        )
        assert len(preview.proposals) == 1
        assert "Investigated connection pool timeout" in preview.proposals[0].summary


def _assert_day_rounding_modes(engine: RollupEngine) -> None:
    """Assert `ceil` and `floor` rounding modes on daily rollup buckets."""
    for mode, expected_first_pair in (
        (ROUNDING_MODE_CEIL, TWENTY_FIVE_MINUTES_SECONDS),
        (ROUNDING_MODE_FLOOR, TWENTY_MINUTES_SECONDS),
    ):
        preview = engine.preview_rollup(
            sink="jira",
            policy=RollupPolicy(
                grouping_window=GROUPING_WINDOW_DAY,
                min_duration_seconds=MIN_DURATION_THRESHOLD,
                rounding_quantum_seconds=QUANTUM_FIVE_MINUTES,
                rounding_mode=mode,
            ),
        )
        assert [p.rounded_duration_seconds for p in preview.proposals] == [
            expected_first_pair,
            expected_first_pair,
            THIRTY_MINUTES_SECONDS,
            THIRTY_MINUTES_SECONDS,
        ]


def test_rollup_grouping_windows_min_duration_and_rounding_modes(tmp_path: Path) -> None:
    """Verify `day`/`week`/`month`/`interval` grouping, `min_duration_seconds`, and rounding."""
    db_path = tmp_path / "windows.sqlite3"
    with Ledger(db_path) as ledger, OutboxStore(db_path) as store:
        _seed_window_intervals(ledger)
        engine = RollupEngine(ledger=ledger, outbox_store=store)
        _assert_day_rounding_modes(engine)

        by_week = engine.preview_rollup(
            sink="jira",
            policy=RollupPolicy(
                grouping_window=GROUPING_WINDOW_WEEK, flush_cadence=FLUSH_CADENCE_EOW
            ),
        )
        by_month = engine.preview_rollup(
            sink="jira", policy=RollupPolicy(grouping_window=GROUPING_WINDOW_MONTH)
        )
        by_interval = engine.preview_rollup(
            sink="jira",
            policy=RollupPolicy(
                grouping_window=GROUPING_WINDOW_INTERVAL,
                min_duration_seconds=MIN_DURATION_THRESHOLD,
            ),
        )
        assert len(by_week.proposals) == EXPECTED_THREE_BUCKETS
        assert len(by_month.proposals) == EXPECTED_TWO_BUCKETS
        assert len(by_interval.proposals) == EXPECTED_FOUR_BUCKETS
        assert by_interval.filtered_short_bucket_count == 1


def test_deterministic_idempotency_key_and_preview_diff_lifecycle(tmp_path: Path) -> None:
    """Verify SHA-256 `idempotency_key` determinism and `PreviewDiff` create/unchanged/update."""
    db_path = tmp_path / "diffs.sqlite3"
    with Ledger(db_path) as ledger, OutboxStore(db_path) as store:
        act = ledger.create_activity("Outbox Diff Engine", external_ref="PROJ-301")
        ledger.record_structured_interval(
            act.id,
            started_at=EPOCH_AUG_03,
            ended_at=EPOCH_AUG_03 + timedelta(seconds=ONE_HOUR_SECONDS),
        )
        policy = RollupPolicy(
            grouping_window=GROUPING_WINDOW_DAY, rounding_quantum_seconds=QUANTUM_ONE_MINUTE
        )
        engine = RollupEngine(ledger=ledger, outbox_store=store)

        first = engine.preview_rollup(sink="jira", policy=policy)
        second = engine.preview_rollup(sink="jira", policy=policy)
        assert first.proposals[0].idempotency_key == second.proposals[0].idempotency_key
        assert (first.diffs[0].change_kind, first.diffs[0].before_seconds) == (
            CHANGE_KIND_CREATE,
            None,
        )
        assert "PROJ-301" in first.format_human()

        staged = engine.stage_rollup(sink="jira", policy=policy)[0]
        assert staged.status == OUTBOX_STATUS_STAGED
        store.approve_item(staged.id)
        OutboxDispatcher(outbox_store=store, sinks=[_RecordingMemorySink(store)]).flush_item(
            staged.id, trigger=FLUSH_TRIGGER_MANUAL
        )

        unchanged = engine.preview_rollup(sink="jira", policy=policy)
        assert (unchanged.diffs[0].change_kind, unchanged.diffs[0].delta_seconds) == (
            CHANGE_KIND_UNCHANGED,
            0,
        )

        ledger.record_structured_interval(
            act.id,
            started_at=EPOCH_AUG_03 + timedelta(hours=3),
            ended_at=EPOCH_AUG_03 + timedelta(hours=3, seconds=THIRTY_MINUTES_SECONDS),
        )
        diff = engine.preview_rollup(sink="jira", policy=policy).diffs[0]
        assert (diff.change_kind, diff.before_seconds, diff.after_seconds, diff.delta_seconds) == (
            CHANGE_KIND_UPDATE,
            ONE_HOUR_SECONDS,
            NINETY_MINUTES_SECONDS,
            THIRTY_MINUTES_SECONDS,
        )
        assert diff.to_dict()["change_kind"] == CHANGE_KIND_UPDATE
        assert "+1800s" in diff.format_human()


# endregion
