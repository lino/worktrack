"""Domain models, constants, and `CalendarBucketMapping` rules for calendar connectors."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from worktrack.connectors.calendar.exceptions import CalendarConfigurationError
from worktrack.core.models import (
    MAX_IDENTIFIER_LENGTH,
    MAX_NAME_LENGTH,
    ensure_utc,
    format_utc_iso,
    normalize_tags,
)

# region Constants
CALENDAR_SOURCE_SLUG: Final = "calendar"
CALENDAR_SINK_SLUG: Final = "calendar"

PROVIDER_ICAL: Final = "ical"
PROVIDER_JMAP: Final = "jmap"
PROVIDER_MS365: Final = "ms365"
ALLOWED_CALENDAR_PROVIDERS: Final[frozenset[str]] = frozenset(
    {PROVIDER_ICAL, PROVIDER_JMAP, PROVIDER_MS365}
)

EVENT_STATUS_CONFIRMED: Final = "confirmed"
EVENT_STATUS_TENTATIVE: Final = "tentative"
EVENT_STATUS_CANCELLED: Final = "cancelled"
ALLOWED_EVENT_STATUSES: Final[frozenset[str]] = frozenset(
    {EVENT_STATUS_CONFIRMED, EVENT_STATUS_TENTATIVE, EVENT_STATUS_CANCELLED}
)

DEFAULT_STANDING_MEETINGS_NAME: Final = "Meetings"
DEFAULT_UNTITLED_MEETING_SUMMARY: Final = "Untitled Meeting"
DEFAULT_BUSY_SUMMARY: Final = "Focus Time"
DEFAULT_CALENDAR_TARGET_REF: Final = "primary"
DEFAULT_JMAP_ACCOUNT_ID: Final = "default"
DEFAULT_TICKET_KEY_PATTERN: Final = r"\b([A-Z][A-Z0-9_]{1,15}-\d{1,7})\b"

CALENDAR_EVENT_TAG_PREFIX: Final = "cal:"
CALENDAR_EVENT_HASH_LENGTH: Final = 16
# endregion


# region Validation
def _validate_calendar_event(event: CalendarEvent) -> None:
    """Validate `CalendarEvent` fields, UTC timestamps, and provider/status values."""
    if not event.uid.strip():
        raise CalendarConfigurationError("CalendarEvent uid must be a non-empty string.")
    if not event.title.strip():
        raise CalendarConfigurationError("CalendarEvent title must be a non-empty string.")
    if event.provider not in ALLOWED_CALENDAR_PROVIDERS:
        raise CalendarConfigurationError(f"Unsupported calendar provider '{event.provider}'.")
    if event.status not in ALLOWED_EVENT_STATUSES:
        raise CalendarConfigurationError(f"Unsupported CalendarEvent status '{event.status}'.")
    try:
        utc_start = ensure_utc(event.started_at, field_name="started_at")
        utc_end = ensure_utc(event.ended_at, field_name="ended_at")
    except Exception as exc:
        raise CalendarConfigurationError(str(exc)) from exc
    if utc_end <= utc_start:
        raise CalendarConfigurationError("CalendarEvent ended_at must be > started_at.")


def _normalize_categories(raw_categories: Sequence[str]) -> tuple[str, ...]:
    """Normalize, lowercase, and deduplicate event category labels."""
    seen: list[str] = []
    for raw in raw_categories:
        cleaned = raw.strip().lower()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


def compute_calendar_event_tag(provider: str, uid: str) -> str:
    """Compute bounded deterministic `Interval` tag for `(provider, uid)`."""
    material = f"{provider.strip().lower()}:{uid.strip()}".encode()
    digest = hashlib.sha256(material).hexdigest()[:CALENDAR_EVENT_HASH_LENGTH]
    return f"{CALENDAR_EVENT_TAG_PREFIX}{digest}"


# endregion


# region Models
@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """Normalized meeting event parsed from iCal (RFC 5545), JMAP (RFC 8984), or MS 365 Graph."""

    uid: str
    title: str
    started_at: datetime
    ended_at: datetime
    status: str = EVENT_STATUS_CONFIRMED
    categories: tuple[str, ...] = ()
    provider: str = PROVIDER_ICAL
    calendar_id: str = DEFAULT_CALENDAR_TARGET_REF
    is_all_day: bool = False

    def __post_init__(self) -> None:
        """Validate timestamps and normalize string/category fields."""
        _validate_calendar_event(self)
        object.__setattr__(self, "uid", self.uid.strip())
        object.__setattr__(self, "title", self.title.strip()[:MAX_NAME_LENGTH])
        object.__setattr__(self, "started_at", ensure_utc(self.started_at))
        object.__setattr__(self, "ended_at", ensure_utc(self.ended_at))
        object.__setattr__(self, "categories", _normalize_categories(self.categories))
        object.__setattr__(self, "calendar_id", self.calendar_id.strip() or "primary")

    @property
    def event_tag(self) -> str:
        """Return deterministic `Interval` tag identifying this `(provider, uid)`."""
        return compute_calendar_event_tag(self.provider, self.uid)

    def to_dict(self) -> dict[str, object]:
        """Serialize `CalendarEvent` into a JSON-compatible dictionary."""
        return {
            "uid": self.uid,
            "title": self.title,
            "started_at": format_utc_iso(self.started_at),
            "ended_at": format_utc_iso(self.ended_at),
            "status": self.status,
            "categories": list(self.categories),
            "provider": self.provider,
            "calendar_id": self.calendar_id,
            "is_all_day": self.is_all_day,
        }


@dataclass(frozen=True, slots=True)
class CalendarBucketRule:
    """Rule mapping matching meetings onto a *Standing* `Activity` and optional Jira key."""

    activity_name: str
    external_ref: str | None = None
    rollup_target_ref: str | None = None
    title_pattern: str | None = None
    categories: Sequence[str] = ()
    tags: Sequence[str] = ()
    _compiled_pattern: re.Pattern[str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate `activity_name`, regex `title_pattern`, and category triggers."""
        clean_name = self.activity_name.strip()[:MAX_NAME_LENGTH]
        if not clean_name:
            raise CalendarConfigurationError("CalendarBucketRule activity_name must be non-empty.")
        clean_cats = _normalize_categories(self.categories)
        clean_pattern = None if self.title_pattern is None else self.title_pattern.strip()
        if not clean_pattern and not clean_cats:
            raise CalendarConfigurationError(
                "CalendarBucketRule requires at least one of title_pattern or categories."
            )
        compiled: re.Pattern[str] | None = None
        if clean_pattern:
            try:
                compiled = re.compile(clean_pattern, re.IGNORECASE)
            except re.error as exc:
                raise CalendarConfigurationError(
                    f"Invalid CalendarBucketRule title_pattern '{clean_pattern}': {exc}"
                ) from exc
        clean_ext = _clean_optional_ref(self.external_ref)
        clean_rollup = _clean_optional_ref(self.rollup_target_ref) or clean_ext
        object.__setattr__(self, "activity_name", clean_name)
        object.__setattr__(self, "external_ref", clean_ext)
        object.__setattr__(self, "rollup_target_ref", clean_rollup)
        object.__setattr__(self, "title_pattern", clean_pattern)
        object.__setattr__(self, "categories", clean_cats)
        object.__setattr__(self, "tags", normalize_tags(self.tags) if self.tags else ())
        object.__setattr__(self, "_compiled_pattern", compiled)

    def matches(self, event: CalendarEvent) -> bool:
        """Return `True` when `event` matches `title_pattern` or `categories`."""
        if self._compiled_pattern is not None and self._compiled_pattern.search(event.title):
            return True
        if self.categories:
            event_cats = set(event.categories)
            if any(cat in event_cats for cat in self.categories):
                return True
        return False


def _clean_optional_ref(raw_ref: str | None) -> str | None:
    """Strip and validate an optional Jira/external reference string."""
    if raw_ref is None:
        return None
    cleaned = raw_ref.strip()
    if not cleaned or len(cleaned) > MAX_IDENTIFIER_LENGTH:
        raise CalendarConfigurationError(
            f"external_ref must be 1..{MAX_IDENTIFIER_LENGTH} characters."
        )
    return cleaned


@dataclass(frozen=True, slots=True)
class CalendarBucketMapping:
    """Configurable policy mapping calendar meetings to *Standing* or *Targeted* `Activities`."""

    rules: Sequence[CalendarBucketRule] = ()
    default_standing_activity_name: str = DEFAULT_STANDING_MEETINGS_NAME
    default_standing_external_ref: str | None = None
    extract_ticket_keys: bool = True
    ticket_key_pattern: str = DEFAULT_TICKET_KEY_PATTERN
    create_missing_targeted_activities: bool = True
    _ticket_regex: re.Pattern[str] = field(
        default=re.compile(DEFAULT_TICKET_KEY_PATTERN), init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Validate default bucket fields and compile `ticket_key_pattern`."""
        clean_default = self.default_standing_activity_name.strip()[:MAX_NAME_LENGTH]
        if not clean_default:
            raise CalendarConfigurationError("default_standing_activity_name must be non-empty.")
        try:
            compiled_ticket = re.compile(self.ticket_key_pattern)
        except re.error as exc:
            raise CalendarConfigurationError(
                f"Invalid ticket_key_pattern '{self.ticket_key_pattern}': {exc}"
            ) from exc
        object.__setattr__(self, "rules", tuple(self.rules))
        object.__setattr__(self, "default_standing_activity_name", clean_default)
        object.__setattr__(
            self,
            "default_standing_external_ref",
            _clean_optional_ref(self.default_standing_external_ref),
        )
        object.__setattr__(self, "_ticket_regex", compiled_ticket)

    def extract_ticket_key(self, title: str) -> str | None:
        """Extract the first matching issue/ticket key (e.g. `PROJ-123`) from `title`."""
        if not self.extract_ticket_keys:
            return None
        match = self._ticket_regex.search(title)
        if match is None:
            return None
        return match.group(1) if match.lastindex else match.group(0)

    def match_standing_rule(self, event: CalendarEvent) -> CalendarBucketRule | None:
        """Return the first `CalendarBucketRule` matching `event`, or `None`."""
        for rule in self.rules:
            if rule.matches(event):
                return rule
        return None


@dataclass(frozen=True, slots=True)
class CalendarSyncResult:
    """Summary counts returned by calendar meeting `Source` sync operations."""

    provider: str
    events_fetched: int
    activities_created: int
    activities_updated: int
    intervals_created: int
    intervals_updated: int
    intervals_skipped: int

    def to_dict(self) -> dict[str, object]:
        """Serialize `CalendarSyncResult` into a JSON-compatible dictionary."""
        return {
            "provider": self.provider,
            "events_fetched": self.events_fetched,
            "activities_created": self.activities_created,
            "activities_updated": self.activities_updated,
            "intervals_created": self.intervals_created,
            "intervals_updated": self.intervals_updated,
            "intervals_skipped": self.intervals_skipped,
        }


@dataclass(frozen=True, slots=True)
class CalendarBusyEvent:
    """Grant-verified opaque busy slot delivered by `CalendarFocusSink`."""

    target_ref: str
    provider: str
    calendar_id: str
    started_at: datetime
    ended_at: datetime
    summary: str
    idempotency_key: str
    outbox_item_id: str
    wire_payload: dict[str, object]


# endregion
