"""Canonical Ubiquitous Language models, constants, and pure validators."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Final

from worktrack.core.exceptions import (
    InvalidAttributionWeightError,
    LedgerValidationError,
)

# region Constants
ACTIVITY_KIND_TARGETED: Final = "targeted"
ACTIVITY_KIND_STANDING: Final = "standing"
ALLOWED_ACTIVITY_KINDS: Final[frozenset[str]] = frozenset(
    {ACTIVITY_KIND_TARGETED, ACTIVITY_KIND_STANDING}
)

CAPTURE_MODE_STRUCTURED: Final = "structured"
CAPTURE_MODE_CHAOTIC: Final = "chaotic"
ALLOWED_CAPTURE_MODES: Final[frozenset[str]] = frozenset(
    {CAPTURE_MODE_STRUCTURED, CAPTURE_MODE_CHAOTIC}
)

OVERLAP_POLICY_FORBID: Final = "forbid"
OVERLAP_POLICY_CLOSE_PREVIOUS: Final = "close_previous"
OVERLAP_POLICY_ALLOW: Final = "allow"
ALLOWED_OVERLAP_POLICIES: Final[frozenset[str]] = frozenset(
    {OVERLAP_POLICY_FORBID, OVERLAP_POLICY_CLOSE_PREVIOUS, OVERLAP_POLICY_ALLOW}
)

ENTITY_KIND_PROJECT: Final = "project"
ENTITY_KIND_COMPONENT: Final = "component"
ENTITY_KIND_CUSTOMER: Final = "customer"
ENTITY_KIND_EPIC: Final = "epic"
ENTITY_KIND_COST_CENTER: Final = "cost_center"
BUILTIN_ENTITY_KINDS: Final[frozenset[str]] = frozenset(
    {
        ENTITY_KIND_PROJECT,
        ENTITY_KIND_COMPONENT,
        ENTITY_KIND_CUSTOMER,
        ENTITY_KIND_EPIC,
        ENTITY_KIND_COST_CENTER,
    }
)

FACET_TYPE_ENUM: Final = "enum"
FACET_TYPE_STRING: Final = "string"
FACET_TYPE_INT: Final = "int"
FACET_TYPE_FLOAT: Final = "float"
FACET_TYPE_BOOL: Final = "bool"
ALLOWED_FACET_VALUE_TYPES: Final[frozenset[str]] = frozenset(
    {
        FACET_TYPE_ENUM,
        FACET_TYPE_STRING,
        FACET_TYPE_INT,
        FACET_TYPE_FLOAT,
        FACET_TYPE_BOOL,
    }
)

BUILTIN_WORKPLACE_FACET_KEY: Final = "workplace"
BUILTIN_WORKPLACE_VALUES: Final[tuple[str, ...]] = (
    "home_office",
    "office",
    "mobile_work",
)
BUILTIN_WORKPLACE_DESCRIPTION: Final = (
    "Daily or interval workplace location for tax and personal reporting."
)

DEFAULT_ACTIVITY_SOURCE: Final = "local"
DEFAULT_CONTEXT_FACT_SOURCE: Final = "manual"

MIN_ATTRIBUTION_WEIGHT_EXCLUSIVE: Final = 0.0
MAX_ATTRIBUTION_WEIGHT: Final = 1.0
WEIGHT_SUM_TOLERANCE: Final = 1e-6

MICROSECONDS_PER_SECOND: Final = 1_000_000
SECONDS_PER_DAY: Final = 86_400
MAX_IDENTIFIER_LENGTH: Final = 128
MAX_TAG_LENGTH: Final = 64
MAX_NAME_LENGTH: Final = 256
MIN_SPLIT_SLICES: Final = 2
ISO_DATE_LENGTH: Final = 10

LOWER_SLUG_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ENTITY_KEY_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

type FacetValue = str | int | float | bool


class UnsetType(Enum):
    """Sentinel enum distinguishing omitted optional arguments from `None`."""

    UNSET = "UNSET"


UNSET: Final = UnsetType.UNSET
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class MigrationRecord:
    """Applied SQLite schema migration audit entry."""

    version: int
    name: str
    checksum: str
    applied_at: datetime


@dataclass(frozen=True, slots=True)
class Activity:
    """Unit of work (*Targeted* item or *Standing* stream) receiving `Intervals`."""

    id: str
    name: str
    kind: str
    external_ref: str | None
    rollup_target_ref: str | None
    source: str
    archived: bool
    tags: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Interval:
    """Local `[started_at, ended_at)` or open `[started_at, ...)` time slice."""

    id: str
    started_at: datetime
    ended_at: datetime | None
    activity_id: str | None
    capture_mode: str
    notes: str | None
    tags: tuple[str, ...]
    created_at: datetime
    updated_at: datetime

    @property
    def is_open(self) -> bool:
        """Return `True` when this `Interval` timer is still running."""
        return self.ended_at is None

    @property
    def is_allocated(self) -> bool:
        """Return `True` when this `Interval` is bound to an `Activity`."""
        return self.activity_id is not None

    @property
    def duration(self) -> timedelta | None:
        """Return `ended_at - started_at`, or `None` while open."""
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    @property
    def duration_seconds(self) -> float | None:
        """Return elapsed duration in seconds, or `None` while open."""
        delta = self.duration
        return None if delta is None else delta.total_seconds()


@dataclass(frozen=True, slots=True)
class IntervalSlice:
    """Contiguous slice specification used when splitting a closed `Interval`."""

    activity_id: str | None
    duration: timedelta | None = None
    ended_at: datetime | None = None
    notes: str | None = None
    tags: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Entity:
    """Non-strict domain graph node (`project`, `component`, `customer`, `epic`, etc.)."""

    id: str
    kind: str
    key: str
    name: str
    metadata: dict[str, object]
    created_at: datetime
    updated_at: datetime

    @property
    def slug(self) -> str:
        """Return canonical `Entity` key slug alias."""
        return self.key


@dataclass(frozen=True, slots=True)
class Attribution:
    """Weighted directed edge (`0.0 < weight <= 1.0`) from `Activity` or `Entity`."""

    id: str
    target_entity_id: str
    weight: float
    source_activity_id: str | None
    source_entity_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EntityAttributionShare:
    """Aggregated duration share attributed to an `Entity` across the DAG."""

    entity: Entity
    attributed_seconds: float
    share_of_kind: float


@dataclass(frozen=True, slots=True)
class Facet:
    """Typed user-configurable or built-in metadata schema definition."""

    key: str
    value_type: str
    allowed_values: tuple[str, ...]
    description: str
    builtin: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ContextFact:
    """Concrete `Facet` value asserted for a calendar date or `Interval`."""

    id: str
    facet_key: str
    value: FacetValue
    scope_date: str | None
    interval_id: str | None
    source: str
    updated_at: datetime


# endregion


# region Validation
def ensure_utc(timestamp: datetime, *, field_name: str = "timestamp") -> datetime:
    """Validate timezone awareness and normalize a `datetime` to UTC."""
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise LedgerValidationError(f"{field_name} must be a timezone-aware datetime.")
    return timestamp.astimezone(UTC)


def format_utc_iso(timestamp: datetime) -> str:
    """Format a timezone-aware `datetime` as fixed-width monotonic UTC ISO-8601."""
    utc_dt = ensure_utc(timestamp)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc_iso(raw_iso: str) -> datetime:
    """Parse a stored UTC ISO-8601 string into a UTC `datetime`."""
    return datetime.fromisoformat(raw_iso).astimezone(UTC)


def compute_duration_us(started_at: datetime, ended_at: datetime) -> int:
    """Compute exact integer microseconds between two validated UTC timestamps."""
    utc_start = ensure_utc(started_at, field_name="started_at")
    utc_end = ensure_utc(ended_at, field_name="ended_at")
    if utc_end <= utc_start:
        raise LedgerValidationError("ended_at must be strictly greater than started_at.")
    delta = utc_end - utc_start
    return (
        delta.days * SECONDS_PER_DAY + delta.seconds
    ) * MICROSECONDS_PER_SECOND + delta.microseconds


def normalize_tags(tags: Sequence[str]) -> tuple[str, ...]:
    """Validate, trim, deduplicate, and sort freeform tags."""
    cleaned: set[str] = set()
    for raw_tag in tags:
        stripped = raw_tag.strip()
        if not stripped or len(stripped) > MAX_TAG_LENGTH:
            raise LedgerValidationError(f"Tag must be 1..{MAX_TAG_LENGTH} non-blank characters.")
        cleaned.add(stripped)
    return tuple(sorted(cleaned))


def validate_non_empty_text(value: str, *, field_name: str, max_len: int = MAX_NAME_LENGTH) -> str:
    """Validate and strip a required bounded text field."""
    stripped = value.strip()
    if not stripped or len(stripped) > max_len:
        raise LedgerValidationError(f"{field_name} must be 1..{max_len} non-blank characters.")
    return stripped


def validate_lower_slug(value: str, *, field_name: str) -> str:
    """Validate a lowercase slug identifier (`kind` or `Facet` key)."""
    stripped = value.strip()
    if not LOWER_SLUG_PATTERN.match(stripped):
        raise LedgerValidationError(
            f"{field_name} '{value}' must match {LOWER_SLUG_PATTERN.pattern}."
        )
    return stripped


def validate_entity_key(value: str) -> str:
    """Validate an `Entity` key/slug identifier."""
    stripped = value.strip()
    if not ENTITY_KEY_PATTERN.match(stripped):
        raise LedgerValidationError(
            f"Entity key '{value}' must match {ENTITY_KEY_PATTERN.pattern}."
        )
    return stripped


def validate_attribution_weight(weight: float) -> float:
    """Verify `0.0 < weight <= 1.0` and finite floating-point representation."""
    if isinstance(weight, bool) or not math.isfinite(weight):
        raise InvalidAttributionWeightError("Attribution weight must be a finite number.")
    numeric = float(weight)
    if not (MIN_ATTRIBUTION_WEIGHT_EXCLUSIVE < numeric <= MAX_ATTRIBUTION_WEIGHT):
        raise InvalidAttributionWeightError(
            f"Attribution weight must satisfy 0.0 < weight <= 1.0, got {numeric}."
        )
    return numeric


def normalize_scope_date(scope_date: str | date) -> str:
    """Validate and normalize a calendar date (`YYYY-MM-DD` or `date`) to ISO text."""
    if isinstance(scope_date, datetime):
        raise LedgerValidationError("scope_date must be a date or 'YYYY-MM-DD' string.")
    if isinstance(scope_date, date):
        return scope_date.isoformat()
    stripped = scope_date.strip()
    if len(stripped) != ISO_DATE_LENGTH:
        raise LedgerValidationError("scope_date must be formatted as 'YYYY-MM-DD'.")
    try:
        parsed = date.fromisoformat(stripped)
    except ValueError as exc:
        raise LedgerValidationError(f"Invalid calendar date '{scope_date}'.") from exc
    return parsed.isoformat()


# endregion
