"""Domain models, strategy constants, and validators for `worktrack.reconciler`."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from worktrack.agenda.validators import serialize_activity, serialize_interval
from worktrack.core.models import (
    Activity,
    Interval,
    IntervalSlice,
    ensure_utc,
    format_utc_iso,
)
from worktrack.reconciler.exceptions import ReconcilerValidationError

# region Constants
RETROSPLIT_STRATEGY_PROPORTIONAL: Final = "proportional"
RETROSPLIT_STRATEGY_EQUAL: Final = "equal"
RETROSPLIT_STRATEGY_WEIGHTS: Final = "weights"
ALLOWED_RETROSPLIT_STRATEGIES: Final[frozenset[str]] = frozenset(
    {
        RETROSPLIT_STRATEGY_PROPORTIONAL,
        RETROSPLIT_STRATEGY_EQUAL,
        RETROSPLIT_STRATEGY_WEIGHTS,
    }
)

GAP_REASON_COMPLETED_ACTIVITY: Final = "completed_activity"
GAP_REASON_CLUE_WITHOUT_INTERVAL: Final = "clue_without_interval"
GAP_REASON_BELOW_MIN_DURATION: Final = "below_min_duration"
ALLOWED_GAP_REASONS: Final[frozenset[str]] = frozenset(
    {
        GAP_REASON_COMPLETED_ACTIVITY,
        GAP_REASON_CLUE_WITHOUT_INTERVAL,
        GAP_REASON_BELOW_MIN_DURATION,
    }
)

COMPLETED_ACTIVITY_TAGS: Final[frozenset[str]] = frozenset({"done", "completed", "closed"})
DEFAULT_MIN_BOOKED_SECONDS: Final = 1
DEFAULT_CLUE_SOURCE: Final = "git"
DEFAULT_CLUE_WEIGHT: Final = 1.0
DEFAULT_EQUAL_WEIGHT: Final = 1.0
ZERO_SECONDS: Final = 0
ZERO_WEIGHT: Final = 0.0
SINGLE_SLICE_COUNT: Final = 1
SHARE_PRECISION_DIGITS: Final = 6
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class ReconcileClueInput:
    """Lightweight ambient clue hint for decoupled plausibility reconciliation."""

    activity_id: str | None = None
    external_ref: str | None = None
    timestamp: datetime | None = None
    source: str = DEFAULT_CLUE_SOURCE
    summary: str | None = None
    weight: float = DEFAULT_CLUE_WEIGHT
    clue_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize `ReconcileClueInput` into a JSON-compatible dictionary."""
        return {
            "clue_id": self.clue_id,
            "activity_id": self.activity_id,
            "external_ref": self.external_ref,
            "timestamp": None if self.timestamp is None else format_utc_iso(self.timestamp),
            "source": self.source,
            "summary": self.summary,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class PlausibilityGap:
    """Detected plausibility gap for an `Activity` lacking booked `Interval` duration."""

    activity: Activity
    booked_seconds: int
    min_booked_seconds: int
    deficit_seconds: int
    clue_count: int
    clue_weight_total: float
    agenda_budget_seconds: int | None
    completed_in_agenda: bool
    reasons: tuple[str, ...]
    clues: tuple[ReconcileClueInput, ...] = ()

    @property
    def activity_id(self) -> str:
        """Return target `Activity` identifier."""
        return self.activity.id

    @property
    def activity_name(self) -> str:
        """Return target `Activity` display name."""
        return self.activity.name

    @property
    def external_ref(self) -> str | None:
        """Return target `Activity` external reference key."""
        return self.activity.external_ref

    @property
    def reason(self) -> str:
        """Return primary gap detection reason."""
        return self.reasons[0] if self.reasons else GAP_REASON_COMPLETED_ACTIVITY

    @property
    def suggested_weight(self) -> float:
        """Return proportional weight hint from clue weights or agenda budget."""
        if self.clue_weight_total > ZERO_WEIGHT:
            return self.clue_weight_total
        if self.agenda_budget_seconds is not None and self.agenda_budget_seconds > ZERO_SECONDS:
            return float(self.agenda_budget_seconds)
        return DEFAULT_EQUAL_WEIGHT

    def to_dict(self) -> dict[str, object]:
        """Serialize `PlausibilityGap` into a JSON-compatible dictionary."""
        return {
            "activity": serialize_activity(self.activity),
            "activity_id": self.activity.id,
            "activity_name": self.activity.name,
            "external_ref": self.activity.external_ref,
            "booked_seconds": self.booked_seconds,
            "min_booked_seconds": self.min_booked_seconds,
            "deficit_seconds": self.deficit_seconds,
            "clue_count": self.clue_count,
            "clue_weight_total": self.clue_weight_total,
            "agenda_budget_seconds": self.agenda_budget_seconds,
            "completed_in_agenda": self.completed_in_agenda,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "suggested_weight": self.suggested_weight,
            "clues": [item.to_dict() for item in self.clues],
        }


@dataclass(frozen=True, slots=True)
class PlausibilityReport:
    """Inspection report pairing plausibility gaps and candidate chaotic `Intervals`."""

    window_start: datetime
    window_end: datetime
    min_booked_seconds: int
    gaps: tuple[PlausibilityGap, ...]
    candidate_intervals: tuple[Interval, ...]
    total_unallocated_seconds: int
    total_chaotic_seconds: int

    @property
    def has_gaps(self) -> bool:
        """Return `True` when at least one `PlausibilityGap` was detected."""
        return bool(self.gaps)

    @property
    def has_candidates(self) -> bool:
        """Return `True` when at least one candidate `Interval` is available."""
        return bool(self.candidate_intervals)

    @property
    def gap_activity_ids(self) -> tuple[str, ...]:
        """Return ordered tuple of `Activity` IDs with detected plausibility gaps."""
        return tuple(gap.activity.id for gap in self.gaps)

    @property
    def candidate_interval_ids(self) -> tuple[str, ...]:
        """Return ordered tuple of candidate `Interval` IDs."""
        return tuple(interval.id for interval in self.candidate_intervals)

    def to_dict(self) -> dict[str, object]:
        """Serialize `PlausibilityReport` into a JSON-compatible dictionary."""
        return {
            "window_start": format_utc_iso(self.window_start),
            "window_end": format_utc_iso(self.window_end),
            "min_booked_seconds": self.min_booked_seconds,
            "has_gaps": self.has_gaps,
            "has_candidates": self.has_candidates,
            "total_unallocated_seconds": self.total_unallocated_seconds,
            "total_chaotic_seconds": self.total_chaotic_seconds,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "candidate_intervals": [
                serialize_interval(interval) for interval in self.candidate_intervals
            ],
        }


@dataclass(frozen=True, slots=True)
class RetroSplitSliceProposal:
    """Single proposed contiguous `[started_at, ended_at)` slice for `RetroSplit`."""

    interval_id: str
    activity_id: str
    activity_name: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    weight: float
    share: float
    notes: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def duration(self) -> timedelta:
        """Return slice duration as a `timedelta`."""
        return self.ended_at - self.started_at

    def to_interval_slice(self) -> IntervalSlice:
        """Convert this proposal slice into a core `IntervalSlice` specification."""
        return IntervalSlice(
            activity_id=self.activity_id,
            ended_at=self.ended_at,
            notes=self.notes,
            tags=self.tags if self.tags else None,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize `RetroSplitSliceProposal` into a JSON-compatible dictionary."""
        return {
            "interval_id": self.interval_id,
            "activity_id": self.activity_id,
            "activity_name": self.activity_name,
            "started_at": format_utc_iso(self.started_at),
            "ended_at": format_utc_iso(self.ended_at),
            "duration_seconds": self.duration_seconds,
            "weight": self.weight,
            "share": round(self.share, SHARE_PRECISION_DIGITS),
            "notes": self.notes,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class RetroSplitProposal:
    """Previewable duration-conserving `RetroSplit` plan across one or more `Intervals`."""

    proposal_id: str
    strategy: str
    source_intervals: tuple[Interval, ...]
    slices: tuple[RetroSplitSliceProposal, ...]
    total_duration_seconds: int
    normalized_weights: dict[str, float]
    created_at: datetime

    @property
    def interval_id(self) -> str:
        """Return primary source `Interval` identifier."""
        return self.source_intervals[0].id if self.source_intervals else ""

    @property
    def interval_ids(self) -> tuple[str, ...]:
        """Return tuple of all source `Interval` identifiers."""
        return tuple(interval.id for interval in self.source_intervals)

    @property
    def original_duration_seconds(self) -> int:
        """Return total duration in seconds of the source `Interval`(s)."""
        return self.total_duration_seconds

    @property
    def allocated_duration_seconds(self) -> int:
        """Return sum of all proposed slice durations in seconds."""
        return sum(item.duration_seconds for item in self.slices)

    @property
    def slice_durations(self) -> tuple[int, ...]:
        """Return ordered tuple of proposed slice durations in seconds."""
        return tuple(item.duration_seconds for item in self.slices)

    @property
    def is_conserved(self) -> bool:
        """Return `True` when proposed slice durations exactly equal source duration."""
        return self.allocated_duration_seconds == self.total_duration_seconds

    def slices_for_interval(self, interval_id: str) -> tuple[RetroSplitSliceProposal, ...]:
        """Return ordered slice proposals belonging to `interval_id`."""
        return tuple(item for item in self.slices if item.interval_id == interval_id)

    def to_dict(self) -> dict[str, object]:
        """Serialize `RetroSplitProposal` into a JSON-compatible dictionary."""
        return {
            "proposal_id": self.proposal_id,
            "strategy": self.strategy,
            "interval_ids": list(self.interval_ids),
            "total_duration_seconds": self.total_duration_seconds,
            "allocated_duration_seconds": self.allocated_duration_seconds,
            "is_conserved": self.is_conserved,
            "normalized_weights": {
                key: round(val, SHARE_PRECISION_DIGITS)
                for key, val in self.normalized_weights.items()
            },
            "slices": [item.to_dict() for item in self.slices],
            "created_at": format_utc_iso(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class RetroSplitResult:
    """Sequence-compatible result returned after atomically applying a `RetroSplitProposal`."""

    proposal_id: str
    strategy: str
    intervals: tuple[Interval, ...]
    total_duration_seconds: int
    applied_at: datetime

    def __len__(self) -> int:
        """Return number of bound/split `Intervals` produced."""
        return len(self.intervals)

    def __iter__(self) -> Iterator[Interval]:
        """Iterate over the resulting `Intervals`."""
        return iter(self.intervals)

    def __getitem__(self, index: int) -> Interval:
        """Return the resulting `Interval` at `index`."""
        return self.intervals[index]

    def to_dict(self) -> dict[str, object]:
        """Serialize `RetroSplitResult` into a JSON-compatible dictionary."""
        return {
            "proposal_id": self.proposal_id,
            "strategy": self.strategy,
            "total_duration_seconds": self.total_duration_seconds,
            "interval_count": len(self.intervals),
            "intervals": [serialize_interval(item) for item in self.intervals],
            "applied_at": format_utc_iso(self.applied_at),
        }


# endregion


# region Validation
def validate_positive_weight(value: float, *, field_name: str = "weight") -> float:
    """Validate that `value` is a finite non-boolean number strictly greater than zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReconcilerValidationError(f"{field_name} must be a finite number.")
    numeric = float(value)
    if numeric <= ZERO_WEIGHT:
        raise ReconcilerValidationError(f"{field_name} must be > 0, got {numeric}.")
    return numeric


def coerce_clue_input(
    raw: ReconcileClueInput | Mapping[str, object] | object,
) -> ReconcileClueInput:
    """Normalize a `ReconcileClueInput`, mapping, or duck-typed `Clue` into `ReconcileClueInput`."""
    if isinstance(raw, ReconcileClueInput):
        validate_positive_weight(raw.weight, field_name="clue.weight")
        ts = None if raw.timestamp is None else ensure_utc(raw.timestamp, field_name="timestamp")
        return ReconcileClueInput(
            activity_id=raw.activity_id,
            external_ref=raw.external_ref,
            timestamp=ts,
            source=raw.source,
            summary=raw.summary,
            weight=float(raw.weight),
            clue_id=raw.clue_id,
        )
    act_id = _opt_str(_first_field(raw, "activity_id"))
    ext_ref = _opt_str(_first_field(raw, "external_ref", "issue_key", "activity_key"))
    if act_id is None and ext_ref is None:
        raise ReconcilerValidationError(
            "Clue hint must provide activity_id, external_ref, or issue_key."
        )
    raw_weight = _first_field(raw, "weight", default=DEFAULT_CLUE_WEIGHT)
    weight = validate_positive_weight(
        float(raw_weight) if isinstance(raw_weight, (int, float)) else -1.0,
        field_name="clue.weight",
    )
    return ReconcileClueInput(
        activity_id=act_id,
        external_ref=ext_ref,
        timestamp=_coerce_optional_timestamp(_first_field(raw, "timestamp", "occurred_at")),
        source=_opt_str(_first_field(raw, "source")) or DEFAULT_CLUE_SOURCE,
        summary=_opt_str(_first_field(raw, "summary", "description")),
        weight=weight,
        clue_id=_opt_str(_first_field(raw, "clue_id", "id")),
    )


def _first_field(raw: object, *keys: str, default: object = None) -> object:
    """Return the first non-None attribute or mapping entry matching `keys`."""
    for key in keys:
        val = raw.get(key) if isinstance(raw, Mapping) else getattr(raw, key, None)
        if val is not None:
            return val
    return default


def _opt_str(value: object) -> str | None:
    """Return stripped non-empty string or `None`."""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _coerce_optional_timestamp(value: object) -> datetime | None:
    """Parse an optional `datetime` or ISO-8601 string into UTC `datetime`."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value, field_name="clue.timestamp")
    if isinstance(value, str):
        try:
            return ensure_utc(datetime.fromisoformat(value.strip()), field_name="clue.timestamp")
        except ValueError as exc:
            raise ReconcilerValidationError(f"Invalid clue timestamp '{value}'.") from exc
    raise ReconcilerValidationError("Clue timestamp must be a datetime or ISO-8601 string.")


# endregion
