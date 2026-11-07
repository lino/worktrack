"""Domain models, constants, and exceptions for `worktrack.agenda`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Final

from worktrack.core.exceptions import LedgerError, LedgerValidationError, NotFoundError
from worktrack.core.models import (
    Activity,
    EntityAttributionShare,
    Interval,
    ensure_utc,
    format_utc_iso,
)

# region Constants
HORIZON_TODAY: Final = "today"
HORIZON_THIS_WEEK: Final = "this_week"
HORIZON_BACKLOG: Final = "backlog"
ALLOWED_AGENDA_HORIZONS: Final[frozenset[str]] = frozenset(
    {HORIZON_TODAY, HORIZON_THIS_WEEK, HORIZON_BACKLOG}
)

AGENDA_STATUS_PLANNED: Final = "planned"
AGENDA_STATUS_IN_PROGRESS: Final = "in_progress"
AGENDA_STATUS_DONE: Final = "done"
AGENDA_STATUS_DEFERRED: Final = "deferred"
ALLOWED_AGENDA_STATUSES: Final[frozenset[str]] = frozenset(
    {
        AGENDA_STATUS_PLANNED,
        AGENDA_STATUS_IN_PROGRESS,
        AGENDA_STATUS_DONE,
        AGENDA_STATUS_DEFERRED,
    }
)
OPEN_AGENDA_STATUSES: Final[frozenset[str]] = frozenset(
    {AGENDA_STATUS_PLANNED, AGENDA_STATUS_IN_PROGRESS}
)


class AgendaPriority(IntEnum):
    """Prioritized execution urgency tiers (`1` = highest urgency)."""

    URGENT = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4
    DEFERRED = 5


DEFAULT_AGENDA_PRIORITY: Final[int] = int(AgendaPriority.NORMAL)
MIN_AGENDA_PRIORITY: Final = 0
MAX_AGENDA_PRIORITY: Final = 100
MIN_BUDGET_VALUE: Final = 0
SECONDS_PER_MINUTE: Final = 60
DAYS_PER_WEEK: Final = 7
DEFAULT_APPROACHING_DEADLINE_HOURS: Final = 48
AGENDA_SCHEMA_VERSION_INITIAL: Final = 1
EMPTY_AGENDA_SCHEMA_VERSION: Final = 0
ZERO_SECONDS_INT: Final = 0

END_OF_DAY_HOUR: Final = 23
END_OF_DAY_MINUTE: Final = 59
END_OF_DAY_SECOND: Final = 59
END_OF_DAY_MICROSECOND: Final = 999_999

METRIC_AGENDA_ITEMS_SCHEDULED_TOTAL: Final = "worktrack_agenda_items_scheduled_total"
UNALLOCATED_ACTIVITY_KEY: Final = "unallocated"
UNALLOCATED_ACTIVITY_NAME: Final = "Unallocated"
DEFAULT_FOCUS_LABEL: Final = "Focus Block"
# endregion


# region Exceptions
class AgendaError(LedgerError):
    """Base exception for `worktrack.agenda` planning and insight operations."""


class AgendaValidationError(AgendaError, LedgerValidationError):
    """Raised when an `AgendaItem` or `FocusBlock` field fails validation."""


class AgendaItemNotFoundError(AgendaError, NotFoundError):
    """Raised when a requested `AgendaItem` identifier does not exist."""


class FocusBlockNotFoundError(AgendaError, NotFoundError):
    """Raised when a requested `FocusBlock` identifier does not exist."""


# endregion


# region Models
@dataclass(frozen=True, slots=True)
class AgendaItem:
    """Prioritized planning item bound to an `Activity` across `today`/`this_week`/`backlog`."""

    id: str
    activity_id: str
    horizon: str
    priority: int
    status: str
    budget_seconds: int | None
    deadline: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def daily_budget_minutes(self) -> int | None:
        """Return integer minutes equivalent of `budget_seconds`, or `None`."""
        if self.budget_seconds is None:
            return None
        return self.budget_seconds // SECONDS_PER_MINUTE

    @property
    def deadline_at(self) -> datetime | None:
        """Return UTC `datetime` deadline boundary, or `None` when unset."""
        from worktrack.agenda.validators import parse_deadline_utc

        return parse_deadline_utc(self.deadline)

    def to_dict(self) -> dict[str, object]:
        """Serialize `AgendaItem` into a JSON-compatible dictionary."""
        return {
            "id": self.id,
            "activity_id": self.activity_id,
            "horizon": self.horizon,
            "priority": self.priority,
            "status": self.status,
            "budget_seconds": self.budget_seconds,
            "daily_budget_minutes": self.daily_budget_minutes,
            "deadline": self.deadline,
            "notes": self.notes,
            "created_at": format_utc_iso(self.created_at),
            "updated_at": format_utc_iso(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class FocusBlock:
    """Timeboxed focus reservation with anti-surveillance `expose_busy` toggle."""

    id: str
    started_at: datetime
    ended_at: datetime
    activity_id: str | None
    label: str
    expose_busy: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def duration_seconds(self) -> int:
        """Return whole duration of this `FocusBlock` in seconds."""
        return max(ZERO_SECONDS_INT, round((self.ended_at - self.started_at).total_seconds()))

    def is_active_at(self, moment: datetime) -> bool:
        """Return `True` when `moment` falls inside `[started_at, ended_at)`."""
        utc_moment = ensure_utc(moment, field_name="moment")
        return self.started_at <= utc_moment < self.ended_at

    def to_dict(self) -> dict[str, object]:
        """Serialize `FocusBlock` into a JSON-compatible dictionary."""
        return {
            "id": self.id,
            "started_at": format_utc_iso(self.started_at),
            "ended_at": format_utc_iso(self.ended_at),
            "duration_seconds": self.duration_seconds,
            "activity_id": self.activity_id,
            "label": self.label,
            "expose_busy": self.expose_busy,
            "notes": self.notes,
            "created_at": format_utc_iso(self.created_at),
            "updated_at": format_utc_iso(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class ActivityDurationSummary:
    """Tracked duration total per `Activity` (or unallocated chaotic bucket) in a window."""

    activity_id: str | None
    activity_key: str
    activity_name: str
    activity_kind: str | None
    tracked_seconds: int
    interval_count: int

    def to_dict(self) -> dict[str, object]:
        """Serialize `ActivityDurationSummary` into a JSON-compatible dictionary."""
        return {
            "activity_id": self.activity_id,
            "activity_key": self.activity_key,
            "activity_name": self.activity_name,
            "activity_kind": self.activity_kind,
            "tracked_seconds": self.tracked_seconds,
            "interval_count": self.interval_count,
        }


@dataclass(frozen=True, slots=True)
class AgendaQueueEntry:
    """Enriched `AgendaItem` entry pairing `Activity`, remaining budget, and deadline urgency."""

    item: AgendaItem
    activity: Activity
    tracked_seconds: int
    remaining_budget_seconds: int | None
    deadline_approaching: bool
    is_overdue: bool

    def to_dict(self) -> dict[str, object]:
        """Serialize `AgendaQueueEntry` into a JSON-compatible dictionary."""
        from worktrack.agenda.validators import activity_display_key, serialize_activity

        return {
            "item": self.item.to_dict(),
            "activity": serialize_activity(self.activity),
            "activity_key": activity_display_key(self.activity),
            "tracked_seconds": self.tracked_seconds,
            "remaining_budget_seconds": self.remaining_budget_seconds,
            "deadline_approaching": self.deadline_approaching,
            "is_overdue": self.is_overdue,
        }


@dataclass(frozen=True, slots=True)
class NowInsight:
    """Point-in-time snapshot answering 'what am I doing right now?'."""

    reference_time: datetime
    date_iso: str
    active_interval: Interval | None
    active_activity: Activity | None
    active_elapsed_seconds: int
    active_focus_block: FocusBlock | None
    upcoming_focus_block: FocusBlock | None
    next_agenda_item: AgendaQueueEntry | None
    today_total_seconds: int
    workplace: str | None

    def to_dict(self) -> dict[str, object]:
        """Serialize `NowInsight` into a JSON-compatible dictionary."""
        from worktrack.agenda.validators import serialize_activity, serialize_interval

        return {
            "reference_time": format_utc_iso(self.reference_time),
            "date_iso": self.date_iso,
            "active_interval": (
                None if self.active_interval is None else serialize_interval(self.active_interval)
            ),
            "active_activity": (
                None if self.active_activity is None else serialize_activity(self.active_activity)
            ),
            "active_elapsed_seconds": self.active_elapsed_seconds,
            "active_focus_block": (
                None if self.active_focus_block is None else self.active_focus_block.to_dict()
            ),
            "upcoming_focus_block": (
                None if self.upcoming_focus_block is None else self.upcoming_focus_block.to_dict()
            ),
            "next_agenda_item": (
                None if self.next_agenda_item is None else self.next_agenda_item.to_dict()
            ),
            "today_total_seconds": self.today_total_seconds,
            "workplace": self.workplace,
        }


@dataclass(frozen=True, slots=True)
class HorizonInsight:
    """Aggregated `today` or `this_week` planning, timer, workplace, and attribution insight."""

    horizon: str
    reference_time: datetime
    date_iso: str
    window_start: datetime
    window_end: datetime
    active_interval: Interval | None
    active_activity: Activity | None
    active_elapsed_seconds: int
    active_focus_block: FocusBlock | None
    upcoming_focus_block: FocusBlock | None
    focus_blocks: tuple[FocusBlock, ...]
    total_tracked_seconds: int
    unallocated_seconds: int
    planned_budget_seconds: int
    activity_totals: tuple[ActivityDurationSummary, ...]
    agenda_queue: tuple[AgendaQueueEntry, ...]
    approaching_deadlines: tuple[AgendaQueueEntry, ...]
    workplace: str | None
    workplace_breakdown: dict[str, int]
    entity_attributions: tuple[EntityAttributionShare, ...]

    def to_dict(self) -> dict[str, object]:
        """Serialize `HorizonInsight` into a JSON-compatible dictionary."""
        from worktrack.agenda.validators import (
            serialize_activity,
            serialize_attribution_share,
            serialize_interval,
        )

        return {
            "horizon": self.horizon,
            "reference_time": format_utc_iso(self.reference_time),
            "date_iso": self.date_iso,
            "window_start": format_utc_iso(self.window_start),
            "window_end": format_utc_iso(self.window_end),
            "active_interval": (
                None if self.active_interval is None else serialize_interval(self.active_interval)
            ),
            "active_activity": (
                None if self.active_activity is None else serialize_activity(self.active_activity)
            ),
            "active_elapsed_seconds": self.active_elapsed_seconds,
            "active_focus_block": (
                None if self.active_focus_block is None else self.active_focus_block.to_dict()
            ),
            "upcoming_focus_block": (
                None if self.upcoming_focus_block is None else self.upcoming_focus_block.to_dict()
            ),
            "focus_blocks": [block.to_dict() for block in self.focus_blocks],
            "total_tracked_seconds": self.total_tracked_seconds,
            "unallocated_seconds": self.unallocated_seconds,
            "planned_budget_seconds": self.planned_budget_seconds,
            "activity_totals": [item.to_dict() for item in self.activity_totals],
            "agenda_queue": [entry.to_dict() for entry in self.agenda_queue],
            "approaching_deadlines": [entry.to_dict() for entry in self.approaching_deadlines],
            "workplace": self.workplace,
            "workplace_breakdown": dict(self.workplace_breakdown),
            "entity_attributions": [
                serialize_attribution_share(share) for share in self.entity_attributions
            ],
        }


TodayInsight = HorizonInsight
WeekInsight = HorizonInsight
# endregion
