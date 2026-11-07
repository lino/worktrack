"""Pure field validators and JSON serializers for `worktrack.agenda`."""

from __future__ import annotations

from datetime import UTC, date, datetime

from worktrack.agenda.models import (
    ALLOWED_AGENDA_HORIZONS,
    ALLOWED_AGENDA_STATUSES,
    END_OF_DAY_HOUR,
    END_OF_DAY_MICROSECOND,
    END_OF_DAY_MINUTE,
    END_OF_DAY_SECOND,
    MAX_AGENDA_PRIORITY,
    MIN_AGENDA_PRIORITY,
    MIN_BUDGET_VALUE,
    SECONDS_PER_MINUTE,
    AgendaPriority,
    AgendaValidationError,
)
from worktrack.core.models import (
    ISO_DATE_LENGTH,
    Activity,
    EntityAttributionShare,
    Interval,
    ensure_utc,
    format_utc_iso,
    parse_utc_iso,
)


def validate_horizon(horizon: str) -> str:
    """Validate and normalize an agenda planning horizon identifier."""
    cleaned = horizon.strip().lower()
    if cleaned not in ALLOWED_AGENDA_HORIZONS:
        allowed = ", ".join(sorted(ALLOWED_AGENDA_HORIZONS))
        raise AgendaValidationError(f"Agenda horizon must be one of: {allowed}.")
    return cleaned


def validate_agenda_status(status: str) -> str:
    """Validate and normalize an `AgendaItem` lifecycle status."""
    cleaned = status.strip().lower()
    if cleaned not in ALLOWED_AGENDA_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_AGENDA_STATUSES))
        raise AgendaValidationError(f"Agenda status must be one of: {allowed}.")
    return cleaned


def validate_priority(priority: int | AgendaPriority) -> int:
    """Validate an integer or `AgendaPriority` tier within allowed bounds."""
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise AgendaValidationError("Agenda priority must be an integer.")
    numeric = int(priority)
    if not (MIN_AGENDA_PRIORITY <= numeric <= MAX_AGENDA_PRIORITY):
        raise AgendaValidationError(
            f"Agenda priority must be between {MIN_AGENDA_PRIORITY} and {MAX_AGENDA_PRIORITY}."
        )
    return numeric


def _validate_non_negative_int(value: int, *, field_name: str) -> int:
    """Validate that `value` is a non-boolean integer `>= 0`."""
    if isinstance(value, bool) or not isinstance(value, int) or value < MIN_BUDGET_VALUE:
        raise AgendaValidationError(f"{field_name} must be an integer >= 0.")
    return value


def resolve_budget_seconds(
    *,
    budget_seconds: int | None = None,
    daily_budget_minutes: int | None = None,
) -> int | None:
    """Resolve optional `budget_seconds` or `daily_budget_minutes` into seconds."""
    if budget_seconds is not None:
        _validate_non_negative_int(budget_seconds, field_name="budget_seconds")
    if daily_budget_minutes is None:
        return budget_seconds
    clean_minutes = _validate_non_negative_int(
        daily_budget_minutes, field_name="daily_budget_minutes"
    )
    from_minutes = clean_minutes * SECONDS_PER_MINUTE
    if budget_seconds is not None and budget_seconds != from_minutes:
        raise AgendaValidationError("Conflicting budget_seconds and daily_budget_minutes values.")
    return from_minutes


def normalize_deadline(deadline: str | date | datetime | None) -> str | None:
    """Normalize an optional calendar `date` or UTC `datetime` deadline into ISO text."""
    if deadline is None:
        return None
    if isinstance(deadline, datetime):
        return format_utc_iso(ensure_utc(deadline, field_name="deadline"))
    if isinstance(deadline, date):
        return deadline.isoformat()
    stripped = deadline.strip()
    if not stripped:
        return None
    if len(stripped) == ISO_DATE_LENGTH:
        try:
            return date.fromisoformat(stripped).isoformat()
        except ValueError as exc:
            raise AgendaValidationError(f"Invalid deadline date '{deadline}'.") from exc
    try:
        parsed_dt = datetime.fromisoformat(stripped)
    except ValueError as exc:
        raise AgendaValidationError(f"Invalid deadline timestamp '{deadline}'.") from exc
    utc_dt = (
        parsed_dt.replace(tzinfo=UTC) if parsed_dt.tzinfo is None else parsed_dt.astimezone(UTC)
    )
    return format_utc_iso(utc_dt)


def parse_deadline_utc(raw_deadline: str | None) -> datetime | None:
    """Convert a stored ISO date or UTC timestamp deadline into a UTC `datetime`."""
    if raw_deadline is None:
        return None
    if len(raw_deadline) == ISO_DATE_LENGTH:
        parsed_date = date.fromisoformat(raw_deadline)
        return datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            END_OF_DAY_HOUR,
            END_OF_DAY_MINUTE,
            END_OF_DAY_SECOND,
            END_OF_DAY_MICROSECOND,
            tzinfo=UTC,
        )
    return parse_utc_iso(raw_deadline)


def activity_display_key(activity: Activity) -> str:
    """Return compact display key (`external_ref` when present, else `name`)."""
    return activity.external_ref if activity.external_ref else activity.name


def serialize_activity(activity: Activity) -> dict[str, object]:
    """Serialize an `Activity` domain record into a JSON-compatible dictionary."""
    return {
        "id": activity.id,
        "name": activity.name,
        "kind": activity.kind,
        "external_ref": activity.external_ref,
        "rollup_target_ref": activity.rollup_target_ref,
        "source": activity.source,
        "archived": activity.archived,
        "tags": list(activity.tags),
        "created_at": format_utc_iso(activity.created_at),
        "updated_at": format_utc_iso(activity.updated_at),
    }


def serialize_interval(interval: Interval) -> dict[str, object]:
    """Serialize an `Interval` domain record into a JSON-compatible dictionary."""
    return {
        "id": interval.id,
        "started_at": format_utc_iso(interval.started_at),
        "ended_at": None if interval.ended_at is None else format_utc_iso(interval.ended_at),
        "duration_seconds": interval.duration_seconds,
        "activity_id": interval.activity_id,
        "capture_mode": interval.capture_mode,
        "notes": interval.notes,
        "tags": list(interval.tags),
        "created_at": format_utc_iso(interval.created_at),
        "updated_at": format_utc_iso(interval.updated_at),
    }


def serialize_attribution_share(share: EntityAttributionShare) -> dict[str, object]:
    """Serialize an `EntityAttributionShare` into a JSON-compatible dictionary."""
    return {
        "entity_id": share.entity.id,
        "kind": share.entity.kind,
        "key": share.entity.key,
        "name": share.entity.name,
        "attributed_seconds": share.attributed_seconds,
        "share_of_kind": share.share_of_kind,
    }
