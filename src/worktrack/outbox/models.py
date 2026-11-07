"""Canonical models and constants for `RollupPolicy`, `Outbox`, and `EgressLedger`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final

from worktrack.core.models import format_utc_iso
from worktrack.outbox.exceptions import OutboxValidationError

# region Constants
GROUPING_WINDOW_DAY: Final = "day"
GROUPING_WINDOW_WEEK: Final = "week"
GROUPING_WINDOW_MONTH: Final = "month"
GROUPING_WINDOW_INTERVAL: Final = "interval"
DEFAULT_GROUPING_WINDOW: Final = GROUPING_WINDOW_DAY
ALLOWED_GROUPING_WINDOWS: Final[frozenset[str]] = frozenset(
    {
        GROUPING_WINDOW_DAY,
        GROUPING_WINDOW_WEEK,
        GROUPING_WINDOW_MONTH,
        GROUPING_WINDOW_INTERVAL,
    }
)

ROUNDING_MODE_NEAREST: Final = "nearest"
ROUNDING_MODE_CEIL: Final = "ceil"
ROUNDING_MODE_FLOOR: Final = "floor"
DEFAULT_ROUNDING_MODE: Final = ROUNDING_MODE_NEAREST
ALLOWED_ROUNDING_MODES: Final[frozenset[str]] = frozenset(
    {ROUNDING_MODE_NEAREST, ROUNDING_MODE_CEIL, ROUNDING_MODE_FLOOR}
)

QUANTUM_ONE_SECOND: Final = 1
QUANTUM_ONE_MINUTE: Final = 60
QUANTUM_FIVE_MINUTES: Final = 300
QUANTUM_FIFTEEN_MINUTES: Final = 900
DEFAULT_ROUNDING_QUANTUM_SECONDS: Final = QUANTUM_ONE_MINUTE
DEFAULT_MIN_DURATION_SECONDS: Final = 0
MIN_POSITIVE_QUANTUM_SECONDS: Final = 1

FLUSH_CADENCE_ON_CLOCK_OUT: Final = "on_clock_out"
FLUSH_CADENCE_EOD: Final = "eod"
FLUSH_CADENCE_EOW: Final = "eow"
FLUSH_CADENCE_EOM: Final = "eom"
FLUSH_CADENCE_MANUAL: Final = "manual"
DEFAULT_FLUSH_CADENCE: Final = FLUSH_CADENCE_EOD
ALLOWED_FLUSH_CADENCES: Final[frozenset[str]] = frozenset(
    {
        FLUSH_CADENCE_ON_CLOCK_OUT,
        FLUSH_CADENCE_EOD,
        FLUSH_CADENCE_EOW,
        FLUSH_CADENCE_EOM,
        FLUSH_CADENCE_MANUAL,
    }
)

FLUSH_TRIGGER_MANUAL: Final = "manual"
FLUSH_TRIGGER_CLOCK_OUT: Final = "clock_out"
FLUSH_TRIGGER_SCHEDULE: Final = "schedule"
ALLOWED_FLUSH_TRIGGERS: Final[frozenset[str]] = frozenset(
    {FLUSH_TRIGGER_MANUAL, FLUSH_TRIGGER_CLOCK_OUT, FLUSH_TRIGGER_SCHEDULE}
)

OUTBOX_STATUS_STAGED: Final = "staged"
OUTBOX_STATUS_APPROVED: Final = "approved"
OUTBOX_STATUS_REJECTED: Final = "rejected"
OUTBOX_STATUS_FLUSHED: Final = "flushed"
OUTBOX_STATUS_FAILED: Final = "failed"
ALLOWED_OUTBOX_STATUSES: Final[frozenset[str]] = frozenset(
    {
        OUTBOX_STATUS_STAGED,
        OUTBOX_STATUS_APPROVED,
        OUTBOX_STATUS_REJECTED,
        OUTBOX_STATUS_FLUSHED,
        OUTBOX_STATUS_FAILED,
    }
)

EGRESS_OUTCOME_OK: Final = "ok"
EGRESS_OUTCOME_SKIPPED_DUPLICATE: Final = "skipped_duplicate"
EGRESS_OUTCOME_ERROR: Final = "error"
ALLOWED_EGRESS_OUTCOMES: Final[frozenset[str]] = frozenset(
    {EGRESS_OUTCOME_OK, EGRESS_OUTCOME_SKIPPED_DUPLICATE, EGRESS_OUTCOME_ERROR}
)

CHANGE_KIND_CREATE: Final = "create"
CHANGE_KIND_UPDATE: Final = "update"
CHANGE_KIND_UNCHANGED: Final = "unchanged"
ALLOWED_CHANGE_KINDS: Final[frozenset[str]] = frozenset(
    {CHANGE_KIND_CREATE, CHANGE_KIND_UPDATE, CHANGE_KIND_UNCHANGED}
)

DEFAULT_REDACTED_SUMMARY_TEMPLATE: Final = "{activity_name}"
DEFAULT_EOD_HOUR_UTC: Final = 17
MIN_HOUR_UTC: Final = 0
MAX_HOUR_UTC: Final = 23
DEFAULT_EOW_WEEKDAY: Final = 4
MIN_WEEKDAY_INDEX: Final = 0
MAX_WEEKDAY_INDEX: Final = 6

FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "intervals",
        "raw_intervals",
        "source_interval_ids",
        "notes",
        "raw_notes",
        "clues",
    }
)
# endregion


# region Validation
def _validate_policy_fields(policy: RollupPolicy) -> Mapping[str, str]:
    """Validate `RollupPolicy` fields and freeze `standing_activity_mappings`."""
    _validate_policy_choices(policy)
    _validate_policy_durations(policy.min_duration_seconds, policy.rounding_quantum_seconds)
    _validate_schedule_bounds(policy.eod_hour_utc, policy.eow_weekday)
    return _freeze_standing_mappings(policy.standing_activity_mappings)


def _validate_policy_choices(policy: RollupPolicy) -> None:
    """Validate grouping window, rounding mode, cadence, and summary template."""
    if policy.grouping_window not in ALLOWED_GROUPING_WINDOWS:
        raise OutboxValidationError(f"Unsupported grouping_window '{policy.grouping_window}'.")
    if policy.rounding_mode not in ALLOWED_ROUNDING_MODES:
        raise OutboxValidationError(f"Unsupported rounding_mode '{policy.rounding_mode}'.")
    if policy.flush_cadence not in ALLOWED_FLUSH_CADENCES:
        raise OutboxValidationError(f"Unsupported flush_cadence '{policy.flush_cadence}'.")
    if not policy.summary_template.strip():
        raise OutboxValidationError("summary_template must be a non-empty string.")
    if policy.redact_notes and "{notes" in policy.summary_template:
        raise OutboxValidationError(
            "summary_template cannot reference '{notes}' while redact_notes=True."
        )


def _validate_policy_durations(min_duration_seconds: int, quantum_seconds: int) -> None:
    """Validate non-boolean integer `min_duration_seconds` and `rounding_quantum_seconds`."""
    if isinstance(min_duration_seconds, bool) or min_duration_seconds < 0:
        raise OutboxValidationError("min_duration_seconds must be an integer >= 0.")
    if isinstance(quantum_seconds, bool) or quantum_seconds < MIN_POSITIVE_QUANTUM_SECONDS:
        raise OutboxValidationError("rounding_quantum_seconds must be an integer >= 1.")


def _validate_schedule_bounds(eod_hour_utc: int, eow_weekday: int) -> None:
    """Validate hour (`0..23`) and ISO weekday (`0..6`) cadence bounds."""
    if isinstance(eod_hour_utc, bool) or not (MIN_HOUR_UTC <= eod_hour_utc <= MAX_HOUR_UTC):
        raise OutboxValidationError("eod_hour_utc must be an integer between 0 and 23.")
    if isinstance(eow_weekday, bool) or not (MIN_WEEKDAY_INDEX <= eow_weekday <= MAX_WEEKDAY_INDEX):
        raise OutboxValidationError("eow_weekday must be an integer between 0 (Mon) and 6 (Sun).")


def _freeze_standing_mappings(raw_mappings: Mapping[str, str]) -> Mapping[str, str]:
    """Normalize and freeze `standing_activity_mappings` dictionary."""
    cleaned: dict[str, str] = {}
    for raw_key, raw_target in raw_mappings.items():
        key = raw_key.strip()
        target = raw_target.strip()
        if not key or not target:
            raise OutboxValidationError(
                "standing_activity_mappings keys and target refs must be non-empty."
            )
        cleaned[key] = target
    return MappingProxyType(cleaned)


# endregion


# region Models
@dataclass(frozen=True, slots=True)
class RollupPolicy:
    """`collectd`-style coarsening, rounding, note-redaction, and cadence policy."""

    grouping_window: str = DEFAULT_GROUPING_WINDOW
    min_duration_seconds: int = DEFAULT_MIN_DURATION_SECONDS
    rounding_quantum_seconds: int = DEFAULT_ROUNDING_QUANTUM_SECONDS
    rounding_mode: str = DEFAULT_ROUNDING_MODE
    redact_notes: bool = True
    summary_template: str = DEFAULT_REDACTED_SUMMARY_TEMPLATE
    standing_activity_mappings: Mapping[str, str] = field(default_factory=dict)
    flush_cadence: str = DEFAULT_FLUSH_CADENCE
    eod_hour_utc: int = DEFAULT_EOD_HOUR_UTC
    eow_weekday: int = DEFAULT_EOW_WEEKDAY

    def __post_init__(self) -> None:
        """Validate policy invariants and freeze standing mappings."""
        frozen_map = _validate_policy_fields(self)
        object.__setattr__(self, "standing_activity_mappings", frozen_map)


@dataclass(frozen=True, slots=True)
class RollupProposal:
    """Deterministic local rollup candidate derived from closed `Intervals`."""

    sink: str
    target_ref: str
    window_start: datetime
    window_end: datetime
    raw_duration_seconds: float
    rounded_duration_seconds: int
    summary: str
    cadence: str
    source_interval_ids: tuple[str, ...]
    idempotency_key: str
    payload_preview: dict[str, object]


@dataclass(frozen=True, slots=True)
class PreviewDiff:
    """Human and JSON before/after diff comparing a `RollupProposal` against prior flushes."""

    sink: str
    target_ref: str
    window_start: datetime
    window_end: datetime
    change_kind: str
    before_seconds: int | None
    after_seconds: int
    delta_seconds: int
    before_summary: str | None
    after_summary: str
    idempotency_key: str
    previous_outbox_item_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize diff into a JSON-compatible dictionary."""
        return {
            "sink": self.sink,
            "target_ref": self.target_ref,
            "window_start": format_utc_iso(self.window_start),
            "window_end": format_utc_iso(self.window_end),
            "change_kind": self.change_kind,
            "before_seconds": self.before_seconds,
            "after_seconds": self.after_seconds,
            "delta_seconds": self.delta_seconds,
            "before_summary": self.before_summary,
            "after_summary": self.after_summary,
            "idempotency_key": self.idempotency_key,
            "previous_outbox_item_id": self.previous_outbox_item_id,
        }

    def format_human(self) -> str:
        """Format a scannable single-line human diff summary."""
        sign = "+" if self.delta_seconds >= 0 else ""
        before_label = "none" if self.before_seconds is None else f"{self.before_seconds}s"
        window_label = (
            f"{self.window_start.date().isoformat()}..{self.window_end.date().isoformat()}"
        )
        return (
            f"[{self.change_kind.upper()}] {self.sink} {self.target_ref} ({window_label}): "
            f"{before_label} -> {self.after_seconds}s ({sign}{self.delta_seconds}s) "
            f'| "{self.after_summary}"'
        )


@dataclass(frozen=True, slots=True)
class RollupPreviewResult:
    """Dry-run `Rollup` + `Outbox` preview containing proposals, diffs, and counts."""

    proposals: tuple[RollupProposal, ...]
    diffs: tuple[PreviewDiff, ...]
    cadence: str
    grouping_window: str
    total_intervals_examined: int
    unmapped_interval_count: int
    filtered_short_bucket_count: int

    def to_dict(self) -> dict[str, object]:
        """Serialize dry-run preview into a structured JSON dictionary."""
        return {
            "cadence": self.cadence,
            "grouping_window": self.grouping_window,
            "total_intervals_examined": self.total_intervals_examined,
            "unmapped_interval_count": self.unmapped_interval_count,
            "filtered_short_bucket_count": self.filtered_short_bucket_count,
            "proposals": [dict(prop.payload_preview) for prop in self.proposals],
            "diffs": [diff.to_dict() for diff in self.diffs],
        }

    def format_human(self) -> str:
        """Render multi-line human-readable preview diff output."""
        if not self.diffs:
            return "No eligible rollup proposals."
        return "\n".join(diff.format_human() for diff in self.diffs)


@dataclass(frozen=True, slots=True)
class OutboxItem:
    """Persisted `Outbox` staging row awaiting user review or cadenced `Sink` flush."""

    id: str
    idempotency_key: str
    sink: str
    target_ref: str
    window_start: datetime
    window_end: datetime
    raw_duration_seconds: float
    rounded_duration_seconds: int
    summary: str
    cadence: str
    status: str
    source_interval_ids: tuple[str, ...]
    payload_preview: dict[str, object]
    payload_sha256: str
    attempt_count: int
    rejection_reason: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EgressRecord:
    """Immutable append-only `EgressLedger` audit receipt for an outbound flush attempt."""

    id: str
    recorded_at: datetime
    outbox_item_id: str
    idempotency_key: str
    sink: str
    destination_uri: str
    payload_sha256: str
    outcome: str
    http_status: int | None
    external_receipt_id: str | None
    trace_id: str
    error_detail: str | None


@dataclass(frozen=True, slots=True)
class SinkWriteReceipt:
    """Receipt returned by a `RollupSink` after writing an approved `OutboxItem`."""

    destination_uri: str
    http_status: int
    external_receipt_id: str


@dataclass(frozen=True, slots=True)
class FlushResult:
    """Result of dispatching a single `OutboxItem` through `OutboxDispatcher`."""

    item: OutboxItem
    outcome: str
    egress_record: EgressRecord


# endregion
