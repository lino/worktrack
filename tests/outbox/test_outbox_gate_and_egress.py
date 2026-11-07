"""Behavioural tests for Outbox bypass guard, cadenced flushes, and `EgressLedger`."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worktrack.config import WorktrackSettings
from worktrack.core import SCHEMA_VERSION_INITIAL, Ledger
from worktrack.outbox import (
    EGRESS_OUTCOME_ERROR,
    EGRESS_OUTCOME_OK,
    EGRESS_OUTCOME_SKIPPED_DUPLICATE,
    FLUSH_CADENCE_EOD,
    FLUSH_CADENCE_EOM,
    FLUSH_CADENCE_EOW,
    FLUSH_CADENCE_MANUAL,
    FLUSH_CADENCE_ON_CLOCK_OUT,
    FLUSH_TRIGGER_CLOCK_OUT,
    FLUSH_TRIGGER_MANUAL,
    FLUSH_TRIGGER_SCHEDULE,
    GROUPING_WINDOW_DAY,
    OUTBOX_SCHEMA_VERSION_INITIAL,
    OUTBOX_STATUS_APPROVED,
    OUTBOX_STATUS_FAILED,
    OUTBOX_STATUS_FLUSHED,
    OUTBOX_STATUS_REJECTED,
    QUANTUM_ONE_MINUTE,
    ApprovedOutboxGrant,
    EgressBypassBlockedError,
    OutboxDispatcher,
    OutboxItem,
    OutboxStore,
    RollupEngine,
    RollupPolicy,
    SinkDeliveryError,
    SinkWriteReceipt,
    enforce_outbox_grant,
    should_flush_for_cadence,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
WEDNESDAY_MORNING = datetime(2026, 8, 5, 10, 0, 0, tzinfo=UTC)
WEDNESDAY_EVENING = datetime(2026, 8, 5, 18, 0, 0, tzinfo=UTC)
FRIDAY_EVENING = datetime(2026, 8, 7, 18, 0, 0, tzinfo=UTC)
MONDAY_LAST_DAY_AUG = datetime(2026, 8, 31, 18, 0, 0, tzinfo=UTC)
ONE_HOUR_SECONDS = 3_600
HTTP_CREATED = 201
HTTP_BAD_GATEWAY = 502
SHA256_HEX_LEN = 64
EXPECTED_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
# endregion


# region Fixtures
class _GuardedTestSink:
    """Configurable test `RollupSink` enforcing `ApprovedOutboxGrant`."""

    sink_name = "jira"

    def __init__(self, telemetry: TelemetryRecorder | None = None, *, fail: bool = False) -> None:
        """Initialize test sink with optional telemetry and failure toggle."""
        self._telemetry = telemetry
        self.fail = fail
        self.calls: list[OutboxItem] = []

    def write_rollup(
        self,
        item: OutboxItem,
        grant: ApprovedOutboxGrant | None = None,
        *,
        source: str = "direct_call",
    ) -> SinkWriteReceipt:
        """Verify sealed grant via `enforce_outbox_grant` and simulate remote delivery."""
        enforce_outbox_grant(
            sink_name=self.sink_name,
            item=item,
            grant=grant,
            telemetry=self._telemetry,
            source=source,
        )
        self.calls.append(item)
        uri = f"https://api.atlassian.com/ex/jira/c1/issue/{item.target_ref}/worklog"
        if self.fail:
            raise SinkDeliveryError(
                "Upstream Jira gateway returned HTTP 502.",
                http_status=HTTP_BAD_GATEWAY,
                destination_uri=uri,
            )
        return SinkWriteReceipt(
            destination_uri=uri,
            http_status=HTTP_CREATED,
            external_receipt_id=f"jira-wl-{len(self.calls)}",
        )


def _stage_single_item(
    ledger: Ledger,
    store: OutboxStore,
    *,
    cadence: str = FLUSH_CADENCE_EOD,
    started_at: datetime = WEDNESDAY_MORNING,
) -> OutboxItem:
    """Create a closed interval and stage a single `OutboxItem`."""
    act = ledger.find_activity_by_external_ref("PROJ-500") or ledger.create_activity(
        "Outbox Gate Task", external_ref="PROJ-500"
    )
    ledger.record_structured_interval(
        act.id,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=ONE_HOUR_SECONDS),
    )
    staged = RollupEngine(ledger=ledger, outbox_store=store).stage_rollup(
        sink="jira",
        policy=RollupPolicy(
            grouping_window=GROUPING_WINDOW_DAY,
            rounding_quantum_seconds=QUANTUM_ONE_MINUTE,
            flush_cadence=cadence,
        ),
    )
    return staged[-1]


# endregion


# region Outbox Gate & Egress Tests
def test_sqlite_outbox_schema_migrations_coexist_with_core_ledger(tmp_path: Path) -> None:
    """Verify `OutboxStore` uses `outbox_schema_migrations` without touching `schema_migrations`."""
    settings = WorktrackSettings.from_env(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home_dir=tmp_path,
    )
    with Ledger.from_settings(settings) as ledger, OutboxStore.from_settings(settings) as store:
        assert ledger.schema_version == SCHEMA_VERSION_INITIAL
        assert store.outbox_schema_version == OUTBOX_SCHEMA_VERSION_INITIAL
        assert len(ledger.applied_migrations()) == 1
        assert len(store.applied_migrations()) == 1

    with sqlite3.connect(settings.db_path) as raw_conn:
        tables = {
            str(row[0])
            for row in raw_conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"schema_migrations", "outbox_schema_migrations", "outbox_items", "egress_ledger"} <= (
        tables
    )


def test_outbox_bypass_guard_blocks_unapproved_missing_and_tampered_grants(
    tmp_path: Path,
) -> None:
    """Verify direct Sink writes without a valid `ApprovedOutboxGrant` fail and emit metrics."""
    db_path = tmp_path / "bypass.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    sink = _GuardedTestSink(telemetry=telemetry)

    with Ledger(db_path) as ledger, OutboxStore(db_path, telemetry=telemetry) as store:
        staged = _stage_single_item(ledger, store)
        with pytest.raises(EgressBypassBlockedError):
            sink.write_rollup(staged, grant=None, source="direct_call")
        with pytest.raises(EgressBypassBlockedError):
            store.issue_grant(staged.id, source="unapproved_item")

        store.reject_item(staged.id, reason="User declined export")
        assert store.get_item(staged.id).status == OUTBOX_STATUS_REJECTED
        with pytest.raises(EgressBypassBlockedError):
            store.issue_grant(staged.id, source="rejected_item")

        approved = store.approve_item(
            _stage_single_item(ledger, store, started_at=WEDNESDAY_MORNING + timedelta(days=1)).id
        )
        valid_grant = store.issue_grant(approved.id)
        tampered = dataclasses.replace(
            approved, payload_preview={**approved.payload_preview, "raw_notes": "leaked"}
        )
        with pytest.raises(EgressBypassBlockedError):
            sink.write_rollup(tampered, grant=valid_grant, source="payload_tampered")

        store.reject_item(approved.id, reason="Revoked after grant")
        with pytest.raises(EgressBypassBlockedError):
            sink.write_rollup(approved, grant=valid_grant, source="revoked_grant")

    assert sink.calls == []
    metrics_text = telemetry.render_metrics_text()
    assert (
        'worktrack_egress_bypass_blocked_total{sink="jira",source="direct_call"} 1.0'
        in metrics_text
    )
    assert (
        'worktrack_egress_bypass_blocked_total{sink="jira",source="payload_tampered"} 1.0'
        in metrics_text
    )


def test_cadence_eligibility_across_all_five_cadences(tmp_path: Path) -> None:
    """Verify `should_flush_for_cadence` and `flush_due` across `on_clock_out..manual`."""
    cases = (
        (FLUSH_CADENCE_MANUAL, WEDNESDAY_EVENING, FLUSH_TRIGGER_SCHEDULE, False),
        (FLUSH_CADENCE_MANUAL, WEDNESDAY_MORNING, FLUSH_TRIGGER_MANUAL, True),
        (FLUSH_CADENCE_ON_CLOCK_OUT, WEDNESDAY_MORNING, FLUSH_TRIGGER_SCHEDULE, False),
        (FLUSH_CADENCE_ON_CLOCK_OUT, WEDNESDAY_MORNING, FLUSH_TRIGGER_CLOCK_OUT, True),
        (FLUSH_CADENCE_EOD, WEDNESDAY_MORNING, FLUSH_TRIGGER_SCHEDULE, False),
        (FLUSH_CADENCE_EOD, WEDNESDAY_EVENING, FLUSH_TRIGGER_SCHEDULE, True),
        (FLUSH_CADENCE_EOW, WEDNESDAY_EVENING, FLUSH_TRIGGER_SCHEDULE, False),
        (FLUSH_CADENCE_EOW, FRIDAY_EVENING, FLUSH_TRIGGER_SCHEDULE, True),
        (FLUSH_CADENCE_EOM, FRIDAY_EVENING, FLUSH_TRIGGER_SCHEDULE, False),
        (FLUSH_CADENCE_EOM, MONDAY_LAST_DAY_AUG, FLUSH_TRIGGER_SCHEDULE, True),
    )
    for cadence, now_dt, trigger, expected in cases:
        actual = should_flush_for_cadence(
            cadence, now=now_dt, reference_time=WEDNESDAY_MORNING, trigger=trigger
        )
        assert actual is expected, f"Expected {expected} for {cadence} at {now_dt} ({trigger})"

    db_path = tmp_path / "cadence.sqlite3"
    with Ledger(db_path) as ledger, OutboxStore(db_path) as store:
        item = _stage_single_item(ledger, store, cadence=FLUSH_CADENCE_EOW)
        store.approve_item(item.id)
        dispatcher = OutboxDispatcher(outbox_store=store, sinks=[_GuardedTestSink()])
        assert dispatcher.flush_due(now=WEDNESDAY_EVENING, trigger=FLUSH_TRIGGER_SCHEDULE) == []
        results = dispatcher.flush_due(now=FRIDAY_EVENING, trigger=FLUSH_TRIGGER_SCHEDULE)
        assert len(results) == 1 and results[0].outcome == EGRESS_OUTCOME_OK


def _assert_egress_metrics_and_immutability(db_path: Path, telemetry: TelemetryRecorder) -> None:
    """Assert flush telemetry counters and SQLite append-only `egress_ledger` triggers."""
    metrics_text = telemetry.render_metrics_text()
    for outcome in (EGRESS_OUTCOME_ERROR, EGRESS_OUTCOME_OK, EGRESS_OUTCOME_SKIPPED_DUPLICATE):
        assert (
            f'worktrack_outbox_flushes_total{{cadence="eod",outcome="{outcome}",sink="jira"}} 1.0'
            in metrics_text
        )

    with sqlite3.connect(db_path) as raw_conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw_conn.execute("UPDATE egress_ledger SET outcome = 'error'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw_conn.execute("DELETE FROM egress_ledger")


def test_idempotent_deduplication_error_handling_and_immutable_egress_ledger(
    tmp_path: Path,
) -> None:
    """Verify retry deduplication, W3C `trace_id`, error receipts, and append-only triggers."""
    db_path = tmp_path / "egress.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    sink = _GuardedTestSink(telemetry=telemetry, fail=True)

    with Ledger(db_path) as ledger, OutboxStore(db_path, telemetry=telemetry) as store:
        item = _stage_single_item(ledger, store, cadence=FLUSH_CADENCE_EOD)
        assert store.approve_item(item.id).status == OUTBOX_STATUS_APPROVED
        dispatcher = OutboxDispatcher(
            outbox_store=store,
            sinks=[sink],
            telemetry=telemetry,
            environ={"FRITZ_TRACE_ID": EXPECTED_TRACE_ID},
        )
        first = dispatcher.flush_item(item.id, trigger=FLUSH_TRIGGER_MANUAL)
        assert first.outcome == EGRESS_OUTCOME_ERROR
        assert (store.get_item(item.id).status, store.get_item(item.id).attempt_count) == (
            OUTBOX_STATUS_FAILED,
            1,
        )

        sink.fail = False
        store.approve_item(item.id)
        second = dispatcher.flush_item(item.id, trigger=FLUSH_TRIGGER_MANUAL)
        third = dispatcher.flush_item(item.id, trigger=FLUSH_TRIGGER_MANUAL)
        assert (second.outcome, third.outcome) == (
            EGRESS_OUTCOME_OK,
            EGRESS_OUTCOME_SKIPPED_DUPLICATE,
        )
        assert (store.get_item(item.id).status, len(sink.calls)) == (OUTBOX_STATUS_FLUSHED, 2)

        records = store.list_egress_records()
        assert [rec.outcome for rec in records] == [
            EGRESS_OUTCOME_ERROR,
            EGRESS_OUTCOME_OK,
            EGRESS_OUTCOME_SKIPPED_DUPLICATE,
        ]
        assert (records[1].external_receipt_id, records[1].trace_id) == (
            "jira-wl-2",
            EXPECTED_TRACE_ID,
        )
        assert len(records[1].payload_sha256) == SHA256_HEX_LEN

    _assert_egress_metrics_and_immutability(db_path, telemetry)


# endregion
