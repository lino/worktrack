"""Cadenced `OutboxDispatcher` coordinating grant issuance, deduplication, and `EgressLedger`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Final

from worktrack.outbox.exceptions import EgressBypassBlockedError, OutboxValidationError
from worktrack.outbox.grants import RollupSink, sanitize_bypass_label
from worktrack.outbox.models import (
    DEFAULT_EOD_HOUR_UTC,
    DEFAULT_EOW_WEEKDAY,
    EGRESS_OUTCOME_ERROR,
    EGRESS_OUTCOME_OK,
    EGRESS_OUTCOME_SKIPPED_DUPLICATE,
    FLUSH_TRIGGER_MANUAL,
    FLUSH_TRIGGER_SCHEDULE,
    OUTBOX_STATUS_APPROVED,
    OUTBOX_STATUS_FAILED,
    OUTBOX_STATUS_FLUSHED,
    EgressRecord,
    FlushResult,
    OutboxItem,
)
from worktrack.outbox.policies import should_flush_for_cadence
from worktrack.outbox.store import OutboxStore
from worktrack.secrets.exceptions import scrub_secrets
from worktrack.telemetry.logging import StructuredEventLogger, resolve_trace_id
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
EVENT_OUTBOX_FLUSH: Final = "outbox.flush.completed"
DISPATCHER_GRANT_SOURCE: Final = "dispatcher"
# endregion


# region Dispatcher
class OutboxDispatcher:
    """Dispatch due `approved` `OutboxItems` to `RollupSinks` and record `EgressLedger`."""

    def __init__(
        self,
        *,
        outbox_store: OutboxStore,
        sinks: Iterable[RollupSink] = (),
        telemetry: TelemetryRecorder | None = None,
        event_logger: StructuredEventLogger | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize dispatcher with `outbox_store`, registered `sinks`, and telemetry."""
        self._store = outbox_store
        self._sinks: dict[str, RollupSink] = {sink.sink_name: sink for sink in sinks}
        self._telemetry = telemetry
        self._event_logger = event_logger
        self._environ = environ

    def register_sink(self, sink: RollupSink) -> None:
        """Register or replace a named `RollupSink` adapter."""
        self._sinks[sink.sink_name] = sink

    def flush_item(
        self,
        item_id: str,
        *,
        trigger: str = FLUSH_TRIGGER_MANUAL,
        trace_id: str | None = None,
    ) -> FlushResult:
        """Flush a single `OutboxItem`, deduplicating retries and recording `EgressLedger`."""
        _ = trigger
        item = self._store.get_item(item_id)
        resolved_trace = resolve_trace_id(environ=self._environ, explicit_trace_id=trace_id)
        prior_ok = self._store.has_flushed_idempotency_key(item.sink, item.idempotency_key)
        if prior_ok is not None or item.status == OUTBOX_STATUS_FLUSHED:
            return self._record_duplicate_skip(item, prior_ok, resolved_trace)

        grant = self._store.issue_grant(item.id, source=DISPATCHER_GRANT_SOURCE)
        sink = self._require_sink(item.sink)
        return self._deliver_with_grant(item, sink, grant, resolved_trace)

    def flush_due(
        self,
        *,
        now: datetime | None = None,
        trigger: str = FLUSH_TRIGGER_SCHEDULE,
        sink: str | None = None,
        cadence: str | None = None,
        eod_hour_utc: int = DEFAULT_EOD_HOUR_UTC,
        eow_weekday: int = DEFAULT_EOW_WEEKDAY,
        trace_id: str | None = None,
    ) -> list[FlushResult]:
        """Flush all `approved` `OutboxItems` whose `cadence` is eligible at `now`."""
        utc_now = datetime.now(UTC) if now is None else now
        approved_items = self._store.list_items(
            status=OUTBOX_STATUS_APPROVED,
            sink=sink,
            cadence=cadence,
        )
        results: list[FlushResult] = []
        for item in approved_items:
            due = should_flush_for_cadence(
                item.cadence,
                now=utc_now,
                reference_time=item.window_start,
                trigger=trigger,
                eod_hour_utc=eod_hour_utc,
                eow_weekday=eow_weekday,
            )
            if due:
                results.append(self.flush_item(item.id, trigger=trigger, trace_id=trace_id))
        return results

    def _deliver_with_grant(
        self,
        item: OutboxItem,
        sink: RollupSink,
        grant: object,
        trace_id: str,
    ) -> FlushResult:
        """Invoke `sink.write_rollup` and persist `ok` or `error` receipt."""
        try:
            receipt = sink.write_rollup(
                item,
                grant,  # type: ignore[arg-type]
                source=DISPATCHER_GRANT_SOURCE,
            )
        except EgressBypassBlockedError:
            raise
        except Exception as exc:
            return self._record_delivery_failure(item, exc, trace_id)

        updated_item, record = self._store.finalize_flush_attempt(
            item=item,
            next_status=OUTBOX_STATUS_FLUSHED,
            increment_attempt=True,
            outcome=EGRESS_OUTCOME_OK,
            destination_uri=receipt.destination_uri,
            http_status=receipt.http_status,
            external_receipt_id=receipt.external_receipt_id,
            trace_id=trace_id,
        )
        self._emit_flush_telemetry(item, EGRESS_OUTCOME_OK, trace_id)
        return FlushResult(item=updated_item, outcome=EGRESS_OUTCOME_OK, egress_record=record)

    def _record_duplicate_skip(
        self,
        item: OutboxItem,
        prior_ok: EgressRecord | None,
        trace_id: str,
    ) -> FlushResult:
        """Record an idempotent `skipped_duplicate` receipt without calling external `Sink`."""
        dest_uri = (
            f"{item.sink}://deduplicated/{item.target_ref}"
            if prior_ok is None
            else prior_ok.destination_uri
        )
        prior_receipt = None if prior_ok is None else prior_ok.external_receipt_id
        updated_item, record = self._store.finalize_flush_attempt(
            item=item,
            next_status=OUTBOX_STATUS_FLUSHED,
            increment_attempt=False,
            outcome=EGRESS_OUTCOME_SKIPPED_DUPLICATE,
            destination_uri=dest_uri,
            http_status=None,
            external_receipt_id=prior_receipt,
            trace_id=trace_id,
        )
        self._emit_flush_telemetry(item, EGRESS_OUTCOME_SKIPPED_DUPLICATE, trace_id)
        return FlushResult(
            item=updated_item,
            outcome=EGRESS_OUTCOME_SKIPPED_DUPLICATE,
            egress_record=record,
        )

    def _record_delivery_failure(
        self,
        item: OutboxItem,
        exc: Exception,
        trace_id: str,
    ) -> FlushResult:
        """Persist `failed` status and scrubbed `error` receipt in `EgressLedger`."""
        raw_status = getattr(exc, "http_status", None)
        http_status = raw_status if isinstance(raw_status, int) else None
        raw_uri = getattr(exc, "destination_uri", None)
        dest_uri = (
            raw_uri if isinstance(raw_uri, str) and raw_uri else f"{item.sink}://{item.target_ref}"
        )
        safe_err = scrub_secrets(str(exc))
        updated_item, record = self._store.finalize_flush_attempt(
            item=item,
            next_status=OUTBOX_STATUS_FAILED,
            increment_attempt=True,
            outcome=EGRESS_OUTCOME_ERROR,
            destination_uri=dest_uri,
            http_status=http_status,
            external_receipt_id=None,
            trace_id=trace_id,
            error_detail=safe_err,
        )
        self._emit_flush_telemetry(item, EGRESS_OUTCOME_ERROR, trace_id)
        return FlushResult(item=updated_item, outcome=EGRESS_OUTCOME_ERROR, egress_record=record)

    def _require_sink(self, sink_name: str) -> RollupSink:
        """Look up registered `RollupSink` or raise `OutboxValidationError`."""
        sink = self._sinks.get(sink_name)
        if sink is None:
            raise OutboxValidationError(f"No RollupSink registered for sink '{sink_name}'.")
        return sink

    def _emit_flush_telemetry(self, item: OutboxItem, outcome: str, trace_id: str) -> None:
        """Record bounded `worktrack_outbox_flushes_total` and optional JSONL trace event."""
        safe_sink = sanitize_bypass_label(item.sink)
        if self._telemetry is not None:
            self._telemetry.record_outbox_flush(
                sink=safe_sink,
                cadence=item.cadence,
                outcome=outcome,
            )
        if self._event_logger is not None:
            self._event_logger.emit(
                EVENT_OUTBOX_FLUSH,
                trace_id=trace_id,
                environ=self._environ,
                sink=safe_sink,
                cadence=item.cadence,
                outcome=outcome,
                outbox_item_id=item.id,
            )


# endregion
