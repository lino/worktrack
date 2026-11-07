"""SQLite persistence operations for `AgendaItem` and `FocusBlock` records."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime

from worktrack.agenda.models import (
    AGENDA_STATUS_DONE,
    AGENDA_STATUS_PLANNED,
    DEFAULT_AGENDA_PRIORITY,
    DEFAULT_FOCUS_LABEL,
    HORIZON_TODAY,
    AgendaItem,
    AgendaItemNotFoundError,
    AgendaPriority,
    AgendaValidationError,
    FocusBlock,
    FocusBlockNotFoundError,
)
from worktrack.agenda.validators import (
    normalize_deadline,
    resolve_budget_seconds,
    validate_agenda_status,
    validate_horizon,
    validate_priority,
)
from worktrack.core.activities import require_active_activity
from worktrack.core.exceptions import DuplicateKeyError, LedgerValidationError
from worktrack.core.models import (
    MAX_IDENTIFIER_LENGTH,
    MAX_NAME_LENGTH,
    UNSET,
    UnsetType,
    compute_duration_us,
    ensure_utc,
    format_utc_iso,
    parse_utc_iso,
    validate_non_empty_text,
)
from worktrack.core.schema import transaction


def _validate_agenda_text(
    value: str,
    *,
    field_name: str,
    max_len: int = MAX_NAME_LENGTH,
) -> str:
    """Validate bounded non-blank agenda text, raising `AgendaValidationError` on failure."""
    try:
        return validate_non_empty_text(value, field_name=field_name, max_len=max_len)
    except LedgerValidationError as exc:
        raise AgendaValidationError(str(exc)) from exc


# region Agenda Items
def insert_agenda_item(
    conn: sqlite3.Connection,
    activity_id: str,
    *,
    horizon: str = HORIZON_TODAY,
    priority: int | AgendaPriority = DEFAULT_AGENDA_PRIORITY,
    status: str = AGENDA_STATUS_PLANNED,
    budget_seconds: int | None = None,
    daily_budget_minutes: int | None = None,
    deadline: str | date | datetime | None = None,
    notes: str | None = None,
    item_id: str | None = None,
) -> AgendaItem:
    """Validate and insert a new `AgendaItem` row bound to an active `Activity`."""
    clean_horizon = validate_horizon(horizon)
    clean_prio = validate_priority(priority)
    clean_status = validate_agenda_status(status)
    clean_budget = resolve_budget_seconds(
        budget_seconds=budget_seconds,
        daily_budget_minutes=daily_budget_minutes,
    )
    clean_deadline = normalize_deadline(deadline)
    resolved_id = (
        str(uuid.uuid4())
        if item_id is None
        else _validate_agenda_text(item_id, field_name="item_id", max_len=MAX_IDENTIFIER_LENGTH)
    )
    now_iso = format_utc_iso(datetime.now(UTC))

    try:
        with transaction(conn):
            act = require_active_activity(conn, activity_id)
            conn.execute(
                """
                INSERT INTO agenda_items
                    (id, activity_id, horizon, priority, status, budget_seconds,
                     deadline, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_id,
                    act.id,
                    clean_horizon,
                    clean_prio,
                    clean_status,
                    clean_budget,
                    clean_deadline,
                    notes,
                    now_iso,
                    now_iso,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateKeyError(f"AgendaItem '{resolved_id}' already exists.") from exc
    return get_agenda_item(conn, resolved_id)


def get_agenda_item(conn: sqlite3.Connection, item_id: str) -> AgendaItem:
    """Fetch a single `AgendaItem` by identifier."""
    row = conn.execute(
        """
        SELECT id, activity_id, horizon, priority, status, budget_seconds,
               deadline, notes, created_at, updated_at
        FROM agenda_items
        WHERE id = ?
        """,
        (item_id.strip(),),
    ).fetchone()
    if row is None:
        raise AgendaItemNotFoundError(f"AgendaItem '{item_id}' does not exist.")
    return _row_to_agenda_item(row)


def update_agenda_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    horizon: str | None = None,
    priority: int | AgendaPriority | None = None,
    status: str | None = None,
    budget_seconds: int | UnsetType | None = UNSET,
    daily_budget_minutes: int | UnsetType | None = UNSET,
    deadline: str | date | datetime | UnsetType | None = UNSET,
    notes: str | UnsetType | None = UNSET,
) -> AgendaItem:
    """Update mutable attributes on an existing `AgendaItem` atomically."""
    with transaction(conn):
        existing = get_agenda_item(conn, item_id)
        next_horizon = existing.horizon if horizon is None else validate_horizon(horizon)
        next_prio = existing.priority if priority is None else validate_priority(priority)
        next_status = existing.status if status is None else validate_agenda_status(status)
        next_budget = _resolve_updated_budget(existing, budget_seconds, daily_budget_minutes)
        next_deadline = existing.deadline if deadline is UNSET else normalize_deadline(deadline)
        next_notes = existing.notes if notes is UNSET else notes
        now_iso = format_utc_iso(datetime.now(UTC))
        conn.execute(
            """
            UPDATE agenda_items
            SET horizon = ?, priority = ?, status = ?, budget_seconds = ?,
                deadline = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_horizon,
                next_prio,
                next_status,
                next_budget,
                next_deadline,
                next_notes,
                now_iso,
                existing.id,
            ),
        )
    return get_agenda_item(conn, item_id)


def _resolve_updated_budget(
    existing: AgendaItem,
    budget_seconds: int | UnsetType | None,
    daily_budget_minutes: int | UnsetType | None,
) -> int | None:
    """Resolve updated `budget_seconds` honouring `UNSET` sentinels."""
    if budget_seconds is UNSET and daily_budget_minutes is UNSET:
        return existing.budget_seconds
    sec_arg = None if budget_seconds is UNSET else budget_seconds
    min_arg = None if daily_budget_minutes is UNSET else daily_budget_minutes
    return resolve_budget_seconds(budget_seconds=sec_arg, daily_budget_minutes=min_arg)


def list_agenda_items(
    conn: sqlite3.Connection,
    *,
    horizon: str | None = None,
    horizons: Sequence[str] | None = None,
    status: str | None = None,
    include_done: bool = True,
    activity_id: str | None = None,
) -> list[AgendaItem]:
    """Query prioritized `AgendaItem` rows matching optional horizon and status filters."""
    clauses, params = _build_agenda_item_filters(
        horizon=horizon,
        horizons=horizons,
        status=status,
        include_done=include_done,
        activity_id=activity_id,
    )
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT id, activity_id, horizon, priority, status, budget_seconds,
               deadline, notes, created_at, updated_at
        FROM agenda_items
        {where_sql}
        ORDER BY priority ASC, deadline IS NULL ASC, deadline ASC, created_at ASC, id ASC
        """,
        params,
    ).fetchall()
    return [_row_to_agenda_item(row) for row in rows]


def _build_agenda_item_filters(
    *,
    horizon: str | None,
    horizons: Sequence[str] | None,
    status: str | None,
    include_done: bool,
    activity_id: str | None,
) -> tuple[list[str], list[object]]:
    """Assemble SQL WHERE clauses and parameters for `list_agenda_items`."""
    clauses: list[str] = []
    params: list[object] = []
    merged_horizons = _merge_horizon_filters(horizon, horizons)
    if merged_horizons:
        placeholders = ", ".join("?" for _ in merged_horizons)
        clauses.append(f"horizon IN ({placeholders})")
        params.extend(merged_horizons)
    if status is not None:
        clauses.append("status = ?")
        params.append(validate_agenda_status(status))
    elif not include_done:
        clauses.append("status != ?")
        params.append(AGENDA_STATUS_DONE)
    if activity_id is not None:
        clauses.append("activity_id = ?")
        params.append(activity_id.strip())
    return clauses, params


def _merge_horizon_filters(
    horizon: str | None,
    horizons: Sequence[str] | None,
) -> list[str]:
    """Combine and validate singular and plural horizon filter arguments."""
    combined: list[str] = []
    if horizon is not None:
        combined.append(validate_horizon(horizon))
    if horizons is not None:
        for item in horizons:
            validated = validate_horizon(item)
            if validated not in combined:
                combined.append(validated)
    return combined


def _row_to_agenda_item(row: sqlite3.Row) -> AgendaItem:
    """Hydrate an `AgendaItem` dataclass from a SQLite row."""
    raw_budget = row["budget_seconds"]
    raw_deadline = row["deadline"]
    raw_notes = row["notes"]
    return AgendaItem(
        id=str(row["id"]),
        activity_id=str(row["activity_id"]),
        horizon=str(row["horizon"]),
        priority=int(row["priority"]),
        status=str(row["status"]),
        budget_seconds=None if raw_budget is None else int(raw_budget),
        deadline=None if raw_deadline is None else str(raw_deadline),
        notes=None if raw_notes is None else str(raw_notes),
        created_at=parse_utc_iso(str(row["created_at"])),
        updated_at=parse_utc_iso(str(row["updated_at"])),
    )


# endregion


# region Focus Blocks
def insert_focus_block(
    conn: sqlite3.Connection,
    *,
    started_at: datetime,
    ended_at: datetime,
    label: str | None = None,
    activity_id: str | None = None,
    expose_busy: bool = False,
    notes: str | None = None,
    block_id: str | None = None,
) -> FocusBlock:
    """Validate and insert a timeboxed `FocusBlock` record."""
    if not isinstance(expose_busy, bool):
        raise AgendaValidationError("expose_busy must be a boolean.")
    try:
        utc_start = ensure_utc(started_at, field_name="started_at")
        utc_end = ensure_utc(ended_at, field_name="ended_at")
        dur_us = compute_duration_us(utc_start, utc_end)
    except LedgerValidationError as exc:
        raise AgendaValidationError(str(exc)) from exc

    resolved_id = (
        str(uuid.uuid4())
        if block_id is None
        else _validate_agenda_text(block_id, field_name="block_id", max_len=MAX_IDENTIFIER_LENGTH)
    )
    now_iso = format_utc_iso(datetime.now(UTC))

    try:
        with transaction(conn):
            clean_act_id, default_label = _resolve_focus_activity(conn, activity_id)
            clean_label = _validate_agenda_text(
                default_label if label is None else label,
                field_name="FocusBlock label",
            )
            conn.execute(
                """
                INSERT INTO focus_blocks
                    (id, started_at, ended_at, duration_us, activity_id, label,
                     expose_busy, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_id,
                    format_utc_iso(utc_start),
                    format_utc_iso(utc_end),
                    dur_us,
                    clean_act_id,
                    clean_label,
                    int(expose_busy),
                    notes,
                    now_iso,
                    now_iso,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateKeyError(f"FocusBlock '{resolved_id}' already exists.") from exc
    return get_focus_block(conn, resolved_id)


def _resolve_focus_activity(
    conn: sqlite3.Connection,
    activity_id: str | None,
) -> tuple[str | None, str]:
    """Resolve optional `Activity` ID and default `FocusBlock` label."""
    if activity_id is None:
        return None, DEFAULT_FOCUS_LABEL
    act = require_active_activity(conn, activity_id)
    return act.id, act.name


def get_focus_block(conn: sqlite3.Connection, block_id: str) -> FocusBlock:
    """Fetch a single `FocusBlock` by identifier."""
    row = conn.execute(
        """
        SELECT id, started_at, ended_at, activity_id, label, expose_busy,
               notes, created_at, updated_at
        FROM focus_blocks
        WHERE id = ?
        """,
        (block_id.strip(),),
    ).fetchone()
    if row is None:
        raise FocusBlockNotFoundError(f"FocusBlock '{block_id}' does not exist.")
    return _row_to_focus_block(row)


def list_focus_blocks(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    activity_id: str | None = None,
) -> list[FocusBlock]:
    """List `FocusBlocks` intersecting optional `[since, until)` bounds."""
    clauses: list[str] = []
    params: list[object] = []
    if since is not None and until is not None and ensure_utc(until) <= ensure_utc(since):
        raise AgendaValidationError("until must be strictly greater than since.")
    if until is not None:
        clauses.append("started_at < ?")
        params.append(format_utc_iso(until))
    if since is not None:
        clauses.append("ended_at > ?")
        params.append(format_utc_iso(since))
    if activity_id is not None:
        clauses.append("activity_id = ?")
        params.append(activity_id.strip())

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT id, started_at, ended_at, activity_id, label, expose_busy,
               notes, created_at, updated_at
        FROM focus_blocks
        {where_sql}
        ORDER BY started_at ASC, id ASC
        """,
        params,
    ).fetchall()
    return [_row_to_focus_block(row) for row in rows]


def delete_focus_block(conn: sqlite3.Connection, block_id: str) -> None:
    """Remove an existing `FocusBlock` by identifier."""
    with transaction(conn):
        existing = get_focus_block(conn, block_id)
        conn.execute("DELETE FROM focus_blocks WHERE id = ?", (existing.id,))


def _row_to_focus_block(row: sqlite3.Row) -> FocusBlock:
    """Hydrate a `FocusBlock` dataclass from a SQLite row."""
    raw_act = row["activity_id"]
    raw_notes = row["notes"]
    return FocusBlock(
        id=str(row["id"]),
        started_at=parse_utc_iso(str(row["started_at"])),
        ended_at=parse_utc_iso(str(row["ended_at"])),
        activity_id=None if raw_act is None else str(raw_act),
        label=str(row["label"]),
        expose_busy=bool(row["expose_busy"]),
        notes=None if raw_notes is None else str(raw_notes),
        created_at=parse_utc_iso(str(row["created_at"])),
        updated_at=parse_utc_iso(str(row["updated_at"])),
    )


# endregion
