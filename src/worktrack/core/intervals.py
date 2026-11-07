"""Structured `Interval` timer lifecycle, queries, and row persistence helpers."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from worktrack.core.activities import require_active_activity
from worktrack.core.exceptions import (
    IntervalNotFoundError,
    IntervalStateError,
    LedgerValidationError,
    NoActiveIntervalError,
    OverlapConflictError,
)
from worktrack.core.models import (
    ALLOWED_CAPTURE_MODES,
    ALLOWED_OVERLAP_POLICIES,
    CAPTURE_MODE_STRUCTURED,
    OVERLAP_POLICY_CLOSE_PREVIOUS,
    OVERLAP_POLICY_FORBID,
    Interval,
    compute_duration_us,
    ensure_utc,
    format_utc_iso,
    normalize_tags,
    parse_utc_iso,
)
from worktrack.core.schema import transaction

# region Constants
SINGLE_OPEN_INTERVAL: Final = 1
# endregion


# region Intervals
def clock_in(
    conn: sqlite3.Connection,
    activity_id: str,
    *,
    started_at: datetime | None = None,
    notes: str | None = None,
    tags: Sequence[str] = (),
    overlap_policy: str = OVERLAP_POLICY_FORBID,
) -> Interval:
    """Start an open structured `Interval` governed by `overlap_policy`."""
    if overlap_policy not in ALLOWED_OVERLAP_POLICIES:
        allowed = ", ".join(sorted(ALLOWED_OVERLAP_POLICIES))
        raise LedgerValidationError(f"overlap_policy must be one of: {allowed}.")
    utc_start = ensure_utc(started_at or datetime.now(UTC), field_name="started_at")
    clean_tags = normalize_tags(tags)
    interval_id = str(uuid.uuid4())
    now_iso = format_utc_iso(datetime.now(UTC))

    with transaction(conn):
        require_active_activity(conn, activity_id)
        _apply_overlap_policy(conn, overlap_policy=overlap_policy, new_start=utc_start)
        insert_interval_row(
            conn,
            interval_id=interval_id,
            started_at=utc_start,
            ended_at=None,
            activity_id=activity_id.strip(),
            capture_mode=CAPTURE_MODE_STRUCTURED,
            notes=notes,
            tags=clean_tags,
            now_iso=now_iso,
        )
    return get_interval(conn, interval_id)


def _apply_overlap_policy(
    conn: sqlite3.Connection,
    *,
    overlap_policy: str,
    new_start: datetime,
) -> None:
    """Enforce `forbid`, `close_previous`, or `allow` against open `Intervals`."""
    open_intervals = get_open_intervals(conn)
    if not open_intervals:
        return
    if overlap_policy == OVERLAP_POLICY_FORBID:
        active_ids = ", ".join(item.id for item in open_intervals)
        raise OverlapConflictError(f"Open Interval(s) already active: {active_ids}.")
    if overlap_policy == OVERLAP_POLICY_CLOSE_PREVIOUS:
        end_iso = format_utc_iso(new_start)
        now_iso = format_utc_iso(datetime.now(UTC))
        for active in open_intervals:
            if new_start <= active.started_at:
                raise OverlapConflictError(
                    "Cannot close previous open Interval at or before its started_at."
                )
            dur_us = compute_duration_us(active.started_at, new_start)
            conn.execute(
                """
                UPDATE intervals
                SET ended_at = ?, duration_us = ?, updated_at = ?
                WHERE id = ?
                """,
                (end_iso, dur_us, now_iso, active.id),
            )


def clock_out(
    conn: sqlite3.Connection,
    *,
    ended_at: datetime | None = None,
    interval_id: str | None = None,
    notes: str | None = None,
    tags: Sequence[str] | None = None,
) -> Interval:
    """Close the active open `Interval` (or explicit `interval_id`) at `ended_at`."""
    utc_end = ensure_utc(ended_at or datetime.now(UTC), field_name="ended_at")
    now_iso = format_utc_iso(datetime.now(UTC))

    with transaction(conn):
        target = _resolve_open_interval(conn, interval_id=interval_id)
        dur_us = compute_duration_us(target.started_at, utc_end)
        next_notes = target.notes if notes is None else notes
        conn.execute(
            """
            UPDATE intervals
            SET ended_at = ?, duration_us = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (format_utc_iso(utc_end), dur_us, next_notes, now_iso, target.id),
        )
        if tags is not None:
            replace_interval_tags(conn, target.id, normalize_tags(tags))
    return get_interval(conn, target.id)


def _resolve_open_interval(
    conn: sqlite3.Connection,
    *,
    interval_id: str | None,
) -> Interval:
    """Locate the single open `Interval` to close during `clock_out`."""
    if interval_id is not None:
        target = get_interval(conn, interval_id)
        if not target.is_open:
            raise IntervalStateError(f"Interval '{target.id}' is already closed.")
        return target
    open_intervals = get_open_intervals(conn)
    if not open_intervals:
        raise NoActiveIntervalError("No active open Interval to clock out.")
    if len(open_intervals) > SINGLE_OPEN_INTERVAL:
        raise OverlapConflictError(
            "Multiple open Intervals active; specify interval_id to clock out."
        )
    return open_intervals[0]


def record_structured_interval(
    conn: sqlite3.Connection,
    activity_id: str,
    *,
    started_at: datetime,
    ended_at: datetime,
    notes: str | None = None,
    tags: Sequence[str] = (),
) -> Interval:
    """Record a completed `[started_at, ended_at)` structured `Interval`."""
    utc_start = ensure_utc(started_at, field_name="started_at")
    utc_end = ensure_utc(ended_at, field_name="ended_at")
    compute_duration_us(utc_start, utc_end)
    clean_tags = normalize_tags(tags)
    interval_id = str(uuid.uuid4())
    now_iso = format_utc_iso(datetime.now(UTC))

    with transaction(conn):
        require_active_activity(conn, activity_id)
        insert_interval_row(
            conn,
            interval_id=interval_id,
            started_at=utc_start,
            ended_at=utc_end,
            activity_id=activity_id.strip(),
            capture_mode=CAPTURE_MODE_STRUCTURED,
            notes=notes,
            tags=clean_tags,
            now_iso=now_iso,
        )
    return get_interval(conn, interval_id)


# endregion


# region Persistence
def get_interval(conn: sqlite3.Connection, interval_id: str) -> Interval:
    """Fetch a single `Interval` by identifier."""
    row = conn.execute(
        """
        SELECT id, started_at, ended_at, activity_id, capture_mode, notes,
               created_at, updated_at
        FROM intervals
        WHERE id = ?
        """,
        (interval_id.strip(),),
    ).fetchone()
    if row is None:
        raise IntervalNotFoundError(f"Interval '{interval_id}' does not exist.")
    tags_map = _load_interval_tags(conn, [str(row["id"])])
    return _row_to_interval(row, tags_map.get(str(row["id"]), ()))


def get_open_intervals(conn: sqlite3.Connection) -> list[Interval]:
    """Return all currently open (`ended_at IS NULL`) `Intervals` ordered by start."""
    rows = conn.execute(
        """
        SELECT id, started_at, ended_at, activity_id, capture_mode, notes,
               created_at, updated_at
        FROM intervals
        WHERE ended_at IS NULL
        ORDER BY started_at ASC, id ASC
        """
    ).fetchall()
    tags_map = _load_interval_tags(conn, [str(row["id"]) for row in rows])
    return [_row_to_interval(row, tags_map.get(str(row["id"]), ())) for row in rows]


def list_intervals(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    activity_id: str | None = None,
    capture_mode: str | None = None,
    include_open: bool = True,
    unallocated_only: bool = False,
    tag: str | None = None,
    tags: Sequence[str] | None = None,
) -> list[Interval]:
    """Query `Intervals` intersecting `[since, until)` and optional attribute filters."""
    clauses, params = _build_interval_query_filters(
        since=since,
        until=until,
        activity_id=activity_id,
        capture_mode=capture_mode,
        include_open=include_open,
        unallocated_only=unallocated_only,
        tag=tag,
        tags=tags,
    )
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT id, started_at, ended_at, activity_id, capture_mode, notes,
               created_at, updated_at
        FROM intervals
        {where_sql}
        ORDER BY started_at ASC, id ASC
        """,
        params,
    ).fetchall()
    tags_map = _load_interval_tags(conn, [str(row["id"]) for row in rows])
    return [_row_to_interval(row, tags_map.get(str(row["id"]), ())) for row in rows]


def _append_time_bounds(
    clauses: list[str],
    params: list[object],
    *,
    since: datetime | None,
    until: datetime | None,
    include_open: bool,
) -> None:
    """Append `[since, until)` and open-state predicates to interval query clauses."""
    if since is not None and until is not None and ensure_utc(until) <= ensure_utc(since):
        raise LedgerValidationError("until must be strictly greater than since.")
    if until is not None:
        clauses.append("started_at < ?")
        params.append(format_utc_iso(until))
    if since is not None:
        clauses.append("(ended_at IS NULL OR ended_at > ?)")
        params.append(format_utc_iso(since))
    if not include_open:
        clauses.append("ended_at IS NOT NULL")


def _build_interval_query_filters(
    *,
    since: datetime | None,
    until: datetime | None,
    activity_id: str | None,
    capture_mode: str | None,
    include_open: bool,
    unallocated_only: bool,
    tag: str | None,
    tags: Sequence[str] | None,
) -> tuple[list[str], list[object]]:
    """Assemble SQL WHERE clauses and bind parameters for `list_intervals`."""
    clauses: list[str] = []
    params: list[object] = []
    _append_time_bounds(clauses, params, since=since, until=until, include_open=include_open)
    if unallocated_only:
        clauses.append("activity_id IS NULL")
    elif activity_id is not None:
        clauses.append("activity_id = ?")
        params.append(activity_id.strip())
    if capture_mode is not None:
        if capture_mode not in ALLOWED_CAPTURE_MODES:
            raise LedgerValidationError(f"Unknown capture_mode '{capture_mode}'.")
        clauses.append("capture_mode = ?")
        params.append(capture_mode)
    for req_tag in _merge_tag_args(tag, tags):
        clauses.append(
            "EXISTS (SELECT 1 FROM interval_tags t "
            "WHERE t.interval_id = intervals.id AND t.tag = ?)"
        )
        params.append(req_tag)
    return clauses, params


def _merge_tag_args(tag: str | None, tags: Sequence[str] | None) -> tuple[str, ...]:
    """Normalize optional singular and plural tag query parameters."""
    raw: list[str] = []
    if tag is not None:
        raw.append(tag)
    if tags is not None:
        raw.extend(tags)
    return normalize_tags(raw) if raw else ()


def insert_interval_row(
    conn: sqlite3.Connection,
    *,
    interval_id: str,
    started_at: datetime,
    ended_at: datetime | None,
    activity_id: str | None,
    capture_mode: str,
    notes: str | None,
    tags: tuple[str, ...],
    now_iso: str,
) -> None:
    """Insert an `intervals` record and any associated `interval_tags`."""
    dur_us = None if ended_at is None else compute_duration_us(started_at, ended_at)
    end_iso = None if ended_at is None else format_utc_iso(ended_at)
    conn.execute(
        """
        INSERT INTO intervals
            (id, started_at, ended_at, duration_us, activity_id, capture_mode, notes,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            interval_id,
            format_utc_iso(started_at),
            end_iso,
            dur_us,
            activity_id,
            capture_mode,
            notes,
            now_iso,
            now_iso,
        ),
    )
    replace_interval_tags(conn, interval_id, tags)


def replace_interval_tags(
    conn: sqlite3.Connection,
    interval_id: str,
    tags: tuple[str, ...],
) -> None:
    """Replace all `interval_tags` rows for `interval_id`."""
    conn.execute("DELETE FROM interval_tags WHERE interval_id = ?", (interval_id,))
    if tags:
        conn.executemany(
            "INSERT INTO interval_tags (interval_id, tag) VALUES (?, ?)",
            [(interval_id, item) for item in tags],
        )


def _load_interval_tags(
    conn: sqlite3.Connection,
    interval_ids: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    """Batch-load sorted tag tuples for a sequence of `Interval` IDs."""
    if not interval_ids:
        return {}
    placeholders = ", ".join("?" for _ in interval_ids)
    rows = conn.execute(
        f"""
        SELECT interval_id, tag
        FROM interval_tags
        WHERE interval_id IN ({placeholders})
        ORDER BY tag ASC
        """,
        list(interval_ids),
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["interval_id"]), []).append(str(row["tag"]))
    return {int_id: tuple(items) for int_id, items in grouped.items()}


def _row_to_interval(row: sqlite3.Row, tags: tuple[str, ...]) -> Interval:
    """Hydrate an `Interval` dataclass from a SQLite row and its tags."""
    raw_end = row["ended_at"]
    raw_act = row["activity_id"]
    raw_notes = row["notes"]
    return Interval(
        id=str(row["id"]),
        started_at=parse_utc_iso(str(row["started_at"])),
        ended_at=None if raw_end is None else parse_utc_iso(str(raw_end)),
        activity_id=None if raw_act is None else str(raw_act),
        capture_mode=str(row["capture_mode"]),
        notes=None if raw_notes is None else str(raw_notes),
        tags=tags,
        created_at=parse_utc_iso(str(row["created_at"])),
        updated_at=parse_utc_iso(str(row["updated_at"])),
    )


# endregion
