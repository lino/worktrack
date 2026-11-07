"""Behavioural tests for `CalendarFocusBlockStaging` and Outbox-guarded `CalendarFocusSink`."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from worktrack.agenda import AgendaPlanner, FocusBlock
from worktrack.connectors.calendar import (
    CALENDAR_SINK_SLUG,
    DEFAULT_BUSY_SUMMARY,
    PROVIDER_ICAL,
    PROVIDER_JMAP,
    PROVIDER_MS365,
    CalendarBusyEvent,
    CalendarConfigurationError,
    CalendarFocusBlockStaging,
    CalendarFocusSink,
    CalendarTransportError,
    verify_outbox_grant,
)
from worktrack.outbox import (
    EGRESS_OUTCOME_ERROR,
    EGRESS_OUTCOME_OK,
    EGRESS_OUTCOME_SKIPPED_DUPLICATE,
    OUTBOX_STATUS_APPROVED,
    OUTBOX_STATUS_FAILED,
    OUTBOX_STATUS_FLUSHED,
    OUTBOX_STATUS_STAGED,
    EgressBypassBlockedError,
    OutboxDispatcher,
    OutboxStore,
    SinkWriteReceipt,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
HTTP_CREATED_STATUS: Final = 201
FOCUS_DURATION_SECONDS: Final = 5400
EXPECTED_SINGLE_ITEM: Final = 1
EXPECTED_TWO_RECORDS: Final = 2
SENSITIVE_NOTE_TEXT: Final = "Confidential acquisition security review notes"
SENSITIVE_LABEL_TEXT: Final = "Project Nightfall Deep Work"
# endregion


# region Fixtures
class FakeCalendarBusyTransport:
    """In-memory `CalendarBusyTransport` test double recording published busy slots."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        """Initialize transport double with optional failure exception."""
        self.fail_with = fail_with
        self.published: list[CalendarBusyEvent] = []

    def publish_busy_slot(self, event: CalendarBusyEvent) -> SinkWriteReceipt:
        """Record `CalendarBusyEvent` and return a deterministic `SinkWriteReceipt`."""
        if self.fail_with is not None:
            raise self.fail_with
        self.published.append(event)
        receipt_num = len(self.published)
        return SinkWriteReceipt(
            destination_uri=f"https://calendar.example.test/{event.provider}/{event.calendar_id}",
            http_status=HTTP_CREATED_STATUS,
            external_receipt_id=f"{event.provider}-busy-{receipt_num}",
        )


def _create_sample_focus_blocks() -> tuple[FocusBlock, FocusBlock]:
    """Schedule one `expose_busy=True` and one `expose_busy=False` `FocusBlock`."""
    with AgendaPlanner(":memory:") as planner:
        exposed = planner.schedule_focus_block(
            started_at=datetime(2026, 11, 7, 14, 0, tzinfo=UTC),
            ended_at=datetime(2026, 11, 7, 15, 30, tzinfo=UTC),
            label=SENSITIVE_LABEL_TEXT,
            expose_busy=True,
            notes=SENSITIVE_NOTE_TEXT,
        )
        private_only = planner.schedule_focus_block(
            started_at=datetime(2026, 11, 7, 16, 0, tzinfo=UTC),
            ended_at=datetime(2026, 11, 7, 17, 0, tzinfo=UTC),
            label="Private Unexposed Focus",
            expose_busy=False,
            notes="Strictly local focus block",
        )
        return exposed, private_only


# endregion


# region Calendar Focus Staging Tests
def test_focus_block_staging_filters_expose_busy_and_redacts_private_notes() -> None:
    """Verify `CalendarFocusBlockStaging` only stages `expose_busy=True` with opaque summary."""
    with OutboxStore(":memory:") as outbox:
        exposed, private_only = _create_sample_focus_blocks()
        staging = CalendarFocusBlockStaging(outbox_store=outbox)

        staged_items = staging.stage_focus_blocks(
            [exposed, private_only],
            target_ref="jmap:primary",
        )
        assert len(staged_items) == EXPECTED_SINGLE_ITEM
        item = staged_items[0]
        assert item.sink == CALENDAR_SINK_SLUG
        assert item.target_ref == "jmap:primary"
        assert item.status == OUTBOX_STATUS_STAGED
        assert item.rounded_duration_seconds == FOCUS_DURATION_SECONDS
        assert item.summary == DEFAULT_BUSY_SUMMARY

        serialized_payload = str(item.payload_preview)
        assert SENSITIVE_NOTE_TEXT not in serialized_payload
        assert SENSITIVE_LABEL_TEXT not in serialized_payload
        assert "notes" not in item.payload_preview
        assert item.payload_preview["show_as"] == "busy"
        assert item.payload_preview["summary"] == DEFAULT_BUSY_SUMMARY

        duplicate = staging.stage_focus_block(exposed, target_ref="jmap:primary")
        assert duplicate.id == item.id

        with pytest.raises(CalendarConfigurationError):
            staging.stage_focus_block(private_only, target_ref="jmap:primary")


# endregion


# region Calendar Focus Sink & Egress Tests
def test_calendar_focus_sink_blocks_unapproved_none_or_tampered_grants(tmp_path: Path) -> None:
    """Verify `CalendarFocusSink` and `verify_outbox_grant` block any Outbox bypass."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    transport = FakeCalendarBusyTransport()
    sink = CalendarFocusSink(transport=transport, telemetry=telemetry)
    assert sink.sink_name == CALENDAR_SINK_SLUG

    with OutboxStore(":memory:", telemetry=telemetry) as outbox:
        exposed, _ = _create_sample_focus_blocks()
        staging = CalendarFocusBlockStaging(outbox_store=outbox)
        staged = staging.stage_focus_block(exposed, target_ref="ms365:primary")

        with pytest.raises(EgressBypassBlockedError):
            sink.write_rollup(staged, None, source="direct_call")

        with pytest.raises(EgressBypassBlockedError):
            verify_outbox_grant(item=staged, grant=None, telemetry=telemetry, source="unit_test")

        with pytest.raises(EgressBypassBlockedError):
            outbox.issue_grant(staged.id, source="premature_call")

        approved = outbox.approve_item(staged.id)
        grant = outbox.issue_grant(approved.id)
        bad_preview = {**approved.payload_preview, "summary": SENSITIVE_NOTE_TEXT}
        tampered = replace(approved, payload_preview=bad_preview)
        with pytest.raises(EgressBypassBlockedError):
            sink.write_rollup(tampered, grant, source="tampered_payload")

        outbox.reject_item(approved.id, reason="User cancelled busy slot")
        with pytest.raises(EgressBypassBlockedError):
            sink.write_rollup(approved, grant, source="revoked_grant")

        assert transport.published == []

    metrics_text = telemetry.render_metrics_text()
    for src in ("direct_call", "tampered_payload", "revoked_grant"):
        assert (
            f'worktrack_egress_bypass_blocked_total{{sink="calendar",source="{src}"}} 1.0'
            in metrics_text
        )


def test_calendar_focus_sink_dispatches_across_jmap_ms365_and_ical_with_receipts(
    tmp_path: Path,
) -> None:
    """Verify end-to-end `OutboxDispatcher` flush and `EgressLedger` SHA-256 receipts."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    transport = FakeCalendarBusyTransport()
    sink = CalendarFocusSink(transport=transport, telemetry=telemetry)

    with OutboxStore(":memory:", telemetry=telemetry) as outbox:
        exposed, _ = _create_sample_focus_blocks()
        staging = CalendarFocusBlockStaging(outbox_store=outbox)
        dispatcher = OutboxDispatcher(
            outbox_store=outbox,
            sinks=[sink],
            telemetry=telemetry,
        )

        for target_ref, expected_provider in (
            ("jmap:work", PROVIDER_JMAP),
            ("ms365:primary", PROVIDER_MS365),
            ("ical:personal", PROVIDER_ICAL),
        ):
            staged = staging.stage_focus_block(exposed, target_ref=target_ref)
            approved = outbox.approve_item(staged.id)
            assert approved.status == OUTBOX_STATUS_APPROVED

            flush_res = dispatcher.flush_item(approved.id)
            assert flush_res.outcome == EGRESS_OUTCOME_OK
            assert flush_res.item.status == OUTBOX_STATUS_FLUSHED
            assert flush_res.egress_record.sink == CALENDAR_SINK_SLUG
            assert flush_res.egress_record.payload_sha256 == approved.payload_sha256
            assert flush_res.egress_record.http_status == HTTP_CREATED_STATUS

            dup_res = dispatcher.flush_item(approved.id)
            assert dup_res.outcome == EGRESS_OUTCOME_SKIPPED_DUPLICATE

            published_event = transport.published[-1]
            assert published_event.provider == expected_provider
            assert published_event.summary == DEFAULT_BUSY_SUMMARY
            assert SENSITIVE_NOTE_TEXT not in str(published_event.wire_payload)

        assert len(transport.published) == 3
        assert transport.published[0].wire_payload["@type"] == "CalendarEvent"
        assert transport.published[0].wire_payload["freeBusyStatus"] == "busy"
        assert transport.published[1].wire_payload["showAs"] == "busy"
        assert "TRANSP:OPAQUE" in str(transport.published[2].wire_payload["ics"])

        egress_records = outbox.list_egress_records(sink=CALENDAR_SINK_SLUG)
        assert len(egress_records) == 6


def test_calendar_focus_sink_records_failed_attempt_on_transport_error(tmp_path: Path) -> None:
    """Verify transport errors transition `OutboxItem` to `failed` and record error receipt."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    failing_transport = FakeCalendarBusyTransport(
        fail_with=CalendarTransportError(
            "CalDAV server timeout",
            http_status=504,
            destination_uri="https://caldav.example.test/calendars/primary",
        )
    )
    sink = CalendarFocusSink(transport=failing_transport, telemetry=telemetry)

    with OutboxStore(":memory:", telemetry=telemetry) as outbox:
        exposed, _ = _create_sample_focus_blocks()
        staging = CalendarFocusBlockStaging(outbox_store=outbox)
        dispatcher = OutboxDispatcher(outbox_store=outbox, sinks=[sink], telemetry=telemetry)

        staged = staging.stage_focus_block(exposed, target_ref="ical:primary")
        outbox.approve_item(staged.id)
        result = dispatcher.flush_item(staged.id)
        assert result.outcome == EGRESS_OUTCOME_ERROR
        assert result.item.status == OUTBOX_STATUS_FAILED
        assert result.egress_record.http_status == 504
        assert "CalDAV server timeout" in (result.egress_record.error_detail or "")


# endregion
