"""`CalendarFocusBlockStaging` and grant-verified `CalendarFocusSink` (`RollupSink`)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final, Protocol

from worktrack.agenda.models import FocusBlock
from worktrack.connectors.calendar.exceptions import (
    CalendarConfigurationError,
    CalendarConnectorError,
)
from worktrack.connectors.calendar.ical_parser import format_ical_busy_event
from worktrack.connectors.calendar.models import (
    ALLOWED_CALENDAR_PROVIDERS,
    CALENDAR_SINK_SLUG,
    DEFAULT_BUSY_SUMMARY,
    DEFAULT_CALENDAR_TARGET_REF,
    PROVIDER_ICAL,
    PROVIDER_JMAP,
    PROVIDER_MS365,
    CalendarBusyEvent,
)
from worktrack.core.models import ensure_utc, format_utc_iso
from worktrack.outbox import (
    ALLOWED_FLUSH_CADENCES,
    FLUSH_CADENCE_MANUAL,
    ApprovedOutboxGrant,
    OutboxItem,
    OutboxStore,
    RollupProposal,
    SinkWriteReceipt,
    compute_payload_sha256,
    enforce_outbox_grant,
)
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
SHOW_AS_BUSY: Final = "busy"
VISIBILITY_PRIVATE: Final = "private"
SECONDS_PER_MINUTE: Final = 60
SECONDS_PER_HOUR: Final = 3600
# endregion


# region Protocols
class CalendarBusyTransport(Protocol):
    """Injectable transport protocol publishing opaque `FocusBlock` busy events."""

    def publish_busy_slot(self, event: CalendarBusyEvent) -> SinkWriteReceipt:
        """Publish an opaque busy calendar event and return a `SinkWriteReceipt`."""
        ...


# endregion


# region Focus Block Staging
def build_focus_block_proposal(
    focus_block: FocusBlock,
    *,
    target_ref: str = DEFAULT_CALENDAR_TARGET_REF,
    busy_summary: str = DEFAULT_BUSY_SUMMARY,
    expose_label: bool = False,
    cadence: str = FLUSH_CADENCE_MANUAL,
) -> RollupProposal:
    """Build a redacted `RollupProposal` for a `FocusBlock` with `expose_busy=True`."""
    if not focus_block.expose_busy:
        raise CalendarConfigurationError(
            f"Cannot stage FocusBlock '{focus_block.id}' with expose_busy=False."
        )
    if cadence not in ALLOWED_FLUSH_CADENCES:
        raise CalendarConfigurationError(f"Unsupported flush cadence '{cadence}'.")
    clean_target = target_ref.strip() or DEFAULT_CALENDAR_TARGET_REF
    summary = _resolve_focus_summary(
        focus_block, busy_summary=busy_summary, expose_label=expose_label
    )
    utc_start = ensure_utc(focus_block.started_at, field_name="started_at")
    utc_end = ensure_utc(focus_block.ended_at, field_name="ended_at")
    start_iso, end_iso = format_utc_iso(utc_start), format_utc_iso(utc_end)
    duration_sec = focus_block.duration_seconds
    idem_key = compute_payload_sha256(
        {
            "sink": CALENDAR_SINK_SLUG,
            "target_ref": clean_target,
            "focus_block_id": focus_block.id,
            "window_start": start_iso,
            "window_end": end_iso,
        }
    )
    payload_preview: dict[str, object] = {
        "sink": CALENDAR_SINK_SLUG,
        "target_ref": clean_target,
        "focus_block_id": focus_block.id,
        "started_at": start_iso,
        "ended_at": end_iso,
        "window_end": end_iso,
        "duration_seconds": duration_sec,
        "summary": summary,
        "show_as": SHOW_AS_BUSY,
        "visibility": VISIBILITY_PRIVATE,
        "idempotency_key": idem_key,
    }
    return RollupProposal(
        sink=CALENDAR_SINK_SLUG,
        target_ref=clean_target,
        window_start=utc_start,
        window_end=utc_end,
        raw_duration_seconds=float(duration_sec),
        rounded_duration_seconds=duration_sec,
        summary=summary,
        cadence=cadence,
        source_interval_ids=(focus_block.id,),
        idempotency_key=idem_key,
        payload_preview=payload_preview,
    )


def _resolve_focus_summary(
    focus_block: FocusBlock,
    *,
    busy_summary: str,
    expose_label: bool,
) -> str:
    """Resolve redacted `FocusBlock` summary string."""
    if expose_label:
        return focus_block.label.strip() or DEFAULT_BUSY_SUMMARY
    return busy_summary.strip() or DEFAULT_BUSY_SUMMARY


class CalendarFocusBlockStaging:
    """Stage `FocusBlocks` with `expose_busy=True` as opaque busy slots in `OutboxStore`."""

    def __init__(
        self,
        *,
        outbox_store: OutboxStore,
        default_target_ref: str = DEFAULT_CALENDAR_TARGET_REF,
        busy_summary: str = DEFAULT_BUSY_SUMMARY,
        expose_label: bool = False,
        default_cadence: str = FLUSH_CADENCE_MANUAL,
    ) -> None:
        """Initialize staging service bound to `outbox_store`."""
        self._store = outbox_store
        self._default_target_ref = default_target_ref.strip() or DEFAULT_CALENDAR_TARGET_REF
        self._busy_summary = busy_summary.strip() or DEFAULT_BUSY_SUMMARY
        self._expose_label = expose_label
        self._default_cadence = default_cadence

    def stage_focus_block(
        self,
        focus_block: FocusBlock,
        *,
        target_ref: str | None = None,
        busy_summary: str | None = None,
        expose_label: bool | None = None,
        cadence: str | None = None,
    ) -> OutboxItem:
        """Stage a single `expose_busy=True` `FocusBlock` idempotently into `OutboxStore`."""
        proposal = build_focus_block_proposal(
            focus_block,
            target_ref=target_ref or self._default_target_ref,
            busy_summary=busy_summary or self._busy_summary,
            expose_label=self._expose_label if expose_label is None else expose_label,
            cadence=cadence or self._default_cadence,
        )
        return self._store.stage_proposal(proposal)

    def stage_focus_blocks(
        self,
        focus_blocks: Sequence[FocusBlock],
        *,
        target_ref: str | None = None,
        busy_summary: str | None = None,
        expose_label: bool | None = None,
        cadence: str | None = None,
    ) -> list[OutboxItem]:
        """Filter `focus_blocks` to `expose_busy=True` and stage each into `OutboxStore`."""
        staged: list[OutboxItem] = []
        for block in focus_blocks:
            if not block.expose_busy:
                continue
            staged.append(
                self.stage_focus_block(
                    block,
                    target_ref=target_ref,
                    busy_summary=busy_summary,
                    expose_label=expose_label,
                    cadence=cadence,
                )
            )
        return staged


# endregion


# region Calendar Focus Sink
def verify_outbox_grant(
    *,
    item: OutboxItem,
    grant: ApprovedOutboxGrant | None,
    sink_name: str = CALENDAR_SINK_SLUG,
    telemetry: TelemetryRecorder | None = None,
    source: str = "direct_call",
) -> None:
    """Verify `ApprovedOutboxGrant` and payload hygiene before calendar busy-slot egress."""
    enforce_outbox_grant(
        sink_name=sink_name,
        item=item,
        grant=grant,
        telemetry=telemetry,
        source=source,
    )


class CalendarFocusSink:
    """Outbox-guarded `RollupSink` publishing `FocusBlocks` as opaque calendar busy slots."""

    def __init__(
        self,
        *,
        transport: CalendarBusyTransport,
        default_provider: str = PROVIDER_ICAL,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        """Initialize `CalendarFocusSink` with `CalendarBusyTransport` and optional telemetry."""
        if default_provider not in ALLOWED_CALENDAR_PROVIDERS:
            raise CalendarConfigurationError(
                f"Unsupported default calendar provider '{default_provider}'."
            )
        self._transport = transport
        self._default_provider = default_provider
        self._telemetry = telemetry

    @property
    def sink_name(self) -> str:
        """Return canonical calendar sink slug (`'calendar'`)."""
        return CALENDAR_SINK_SLUG

    def write_rollup(
        self,
        item: OutboxItem,
        grant: ApprovedOutboxGrant | None = None,
        *,
        source: str = "direct_call",
    ) -> SinkWriteReceipt:
        """Verify `ApprovedOutboxGrant` and deliver an opaque busy slot via `transport`."""
        verify_outbox_grant(
            item=item,
            grant=grant,
            sink_name=self.sink_name,
            telemetry=self._telemetry,
            source=source,
        )
        provider, calendar_id = _parse_target_ref(item.target_ref, self._default_provider)
        summary = item.summary.strip() or DEFAULT_BUSY_SUMMARY
        wire_payload = build_busy_wire_payload(
            provider=provider,
            uid=f"wt-focus-{item.idempotency_key[:24]}",
            started_at=item.window_start,
            ended_at=item.window_end,
            summary=summary,
            idempotency_key=item.idempotency_key,
        )
        busy_event = CalendarBusyEvent(
            target_ref=item.target_ref,
            provider=provider,
            calendar_id=calendar_id,
            started_at=item.window_start,
            ended_at=item.window_end,
            summary=summary,
            idempotency_key=item.idempotency_key,
            outbox_item_id=item.id,
            wire_payload=wire_payload,
        )
        receipt = self._transport.publish_busy_slot(busy_event)
        if not receipt.external_receipt_id.strip():
            raise CalendarConnectorError(
                f"CalendarBusyTransport returned empty external_receipt_id for '{item.id}'.",
                http_status=receipt.http_status,
                destination_uri=receipt.destination_uri,
            )
        return receipt


def _parse_target_ref(target_ref: str, default_provider: str) -> tuple[str, str]:
    """Parse `'<provider>:<calendar_id>'` or `'<calendar_id>'` into `(provider, calendar_id)`."""
    cleaned = target_ref.strip() or DEFAULT_CALENDAR_TARGET_REF
    if ":" in cleaned:
        prefix, rest = cleaned.split(":", 1)
        cand = prefix.strip().lower()
        if cand in ALLOWED_CALENDAR_PROVIDERS:
            return cand, rest.strip() or DEFAULT_CALENDAR_TARGET_REF
    return default_provider, cleaned


def build_busy_wire_payload(
    *,
    provider: str,
    uid: str,
    started_at: datetime,
    ended_at: datetime,
    summary: str,
    idempotency_key: str,
) -> dict[str, object]:
    """Build provider-specific opaque busy-slot payload for JMAP, MS 365, or iCal/CalDAV."""
    utc_start = ensure_utc(started_at, field_name="started_at")
    utc_end = ensure_utc(ended_at, field_name="ended_at")
    duration_sec = max(1, round((utc_end - utc_start).total_seconds()))

    if provider == PROVIDER_JMAP:
        return {
            "@type": "CalendarEvent",
            "uid": uid,
            "title": summary,
            "start": utc_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeZone": "UTC",
            "duration": _format_iso_duration_seconds(duration_sec),
            "status": "confirmed",
            "freeBusyStatus": SHOW_AS_BUSY,
            "privacy": VISIBILITY_PRIVATE,
        }
    if provider == PROVIDER_MS365:
        return {
            "subject": summary,
            "start": {
                "dateTime": utc_start.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": utc_end.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                "timeZone": "UTC",
            },
            "showAs": SHOW_AS_BUSY,
            "sensitivity": VISIBILITY_PRIVATE,
            "isAllDay": False,
            "transactionId": idempotency_key,
        }
    return {
        "format": PROVIDER_ICAL,
        "uid": uid,
        "ics": format_ical_busy_event(
            uid=uid,
            started_at=utc_start,
            ended_at=utc_end,
            summary=summary,
        ),
    }


def _format_iso_duration_seconds(duration_seconds: int) -> str:
    """Format a positive integer duration in seconds as an ISO-8601 `PT...` string."""
    hours, rem = divmod(duration_seconds, SECONDS_PER_HOUR)
    minutes, seconds = divmod(rem, SECONDS_PER_MINUTE)
    parts: list[str] = ["PT"]
    if hours > 0:
        parts.append(f"{hours}H")
    if minutes > 0:
        parts.append(f"{minutes}M")
    if seconds > 0 or len(parts) == 1:
        parts.append(f"{seconds}S")
    return "".join(parts)


# endregion
