"""Chaotic unallocated `Interval` capture, retroactive binding, and atomic splitting."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from worktrack.core.activities import require_active_activity
from worktrack.core.exceptions import IntervalStateError, LedgerValidationError
from worktrack.core.facets import clone_interval_context_facts
from worktrack.core.intervals import get_interval, insert_interval_row, replace_interval_tags
from worktrack.core.models import (
    CAPTURE_MODE_CHAOTIC,
    MIN_SPLIT_SLICES,
    Interval,
    IntervalSlice,
    compute_duration_us,
    ensure_utc,
    format_utc_iso,
    normalize_tags,
)
from worktrack.core.schema import transaction

# region Constants
TUPLE_SLICE_TWO_LEN: Final = 2
TUPLE_SLICE_THREE_LEN: Final = 3

type RawIntervalSlice = (
    IntervalSlice
    | tuple[str | None, timedelta | datetime]
    | tuple[str | None, timedelta | datetime, str | None]
)
# endregion


# region Intervals
def record_chaotic_interval(
    conn: sqlite3.Connection,
    *,
    started_at: datetime,
    ended_at: datetime,
    notes: str | None = None,
    tags: Sequence[str] = (),
    activity_id: str | None = None,
) -> Interval:
    """Record a chaotic `[started_at, ended_at)` block (unallocated by default)."""
    utc_start = ensure_utc(started_at, field_name="started_at")
    utc_end = ensure_utc(ended_at, field_name="ended_at")
    compute_duration_us(utc_start, utc_end)
    clean_tags = normalize_tags(tags)
    interval_id = str(uuid.uuid4())
    now_iso = format_utc_iso(datetime.now(UTC))

    with transaction(conn):
        clean_act_id: str | None = None
        if activity_id is not None:
            clean_act_id = require_active_activity(conn, activity_id).id
        insert_interval_row(
            conn,
            interval_id=interval_id,
            started_at=utc_start,
            ended_at=utc_end,
            activity_id=clean_act_id,
            capture_mode=CAPTURE_MODE_CHAOTIC,
            notes=notes,
            tags=clean_tags,
            now_iso=now_iso,
        )
    return get_interval(conn, interval_id)


def bind_interval(
    conn: sqlite3.Connection,
    interval_id: str,
    activity_id: str,
    *,
    notes: str | None = None,
    tags: Sequence[str] | None = None,
    allow_rebind: bool = False,
) -> Interval:
    """Bind an unallocated `Interval` (or rebind when opted in) to an `Activity`."""
    now_iso = format_utc_iso(datetime.now(UTC))
    with transaction(conn):
        current = get_interval(conn, interval_id)
        if current.activity_id is not None and not allow_rebind:
            raise IntervalStateError(
                f"Interval '{current.id}' is already bound; pass allow_rebind=True."
            )
        target_act = require_active_activity(conn, activity_id)
        next_notes = current.notes if notes is None else notes
        conn.execute(
            """
            UPDATE intervals
            SET activity_id = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (target_act.id, next_notes, now_iso, current.id),
        )
        if tags is not None:
            replace_interval_tags(conn, current.id, normalize_tags(tags))
    return get_interval(conn, interval_id)


def split_interval(
    conn: sqlite3.Connection,
    interval_id: str,
    slices: Sequence[RawIntervalSlice],
) -> list[Interval]:
    """Atomically split a closed `Interval` into contiguous duration-preserving slices."""
    specs = [_coerce_slice(item) for item in slices]
    if len(specs) < MIN_SPLIT_SLICES:
        raise LedgerValidationError(f"split_interval requires >= {MIN_SPLIT_SLICES} slices.")
    now_iso = format_utc_iso(datetime.now(UTC))

    with transaction(conn):
        parent = get_interval(conn, interval_id)
        if parent.ended_at is None:
            raise IntervalStateError("Cannot split an open Interval; clock out first.")
        planned = _plan_contiguous_slices(conn, parent, specs)
        child_ids: list[str] = []
        for slice_start, slice_end, act_id, slice_notes, slice_tags in planned:
            child_id = str(uuid.uuid4())
            child_ids.append(child_id)
            insert_interval_row(
                conn,
                interval_id=child_id,
                started_at=slice_start,
                ended_at=slice_end,
                activity_id=act_id,
                capture_mode=parent.capture_mode,
                notes=slice_notes,
                tags=slice_tags,
                now_iso=now_iso,
            )
        clone_interval_context_facts(conn, parent.id, child_ids)
        conn.execute("DELETE FROM intervals WHERE id = ?", (parent.id,))
    return [get_interval(conn, cid) for cid in child_ids]


# endregion


# region Validation
def _coerce_slice(raw: RawIntervalSlice) -> IntervalSlice:
    """Normalize an `IntervalSlice` or `(activity_id, span[, notes])` tuple."""
    if isinstance(raw, IntervalSlice):
        return raw
    if len(raw) not in (TUPLE_SLICE_TWO_LEN, TUPLE_SLICE_THREE_LEN):
        raise LedgerValidationError("Tuple slice must have 2 or 3 elements.")
    act_id = raw[0]
    span = raw[1]
    notes = raw[2] if len(raw) == TUPLE_SLICE_THREE_LEN else None
    if isinstance(span, timedelta):
        return IntervalSlice(activity_id=act_id, duration=span, notes=notes)
    if isinstance(span, datetime):
        return IntervalSlice(activity_id=act_id, ended_at=span, notes=notes)
    raise LedgerValidationError("Slice span must be a timedelta or UTC datetime.")


def _plan_contiguous_slices(
    conn: sqlite3.Connection,
    parent: Interval,
    specs: Sequence[IntervalSlice],
) -> list[tuple[datetime, datetime, str | None, str | None, tuple[str, ...]]]:
    """Validate slice activities, monotonicity, and exact total duration conservation."""
    assert parent.ended_at is not None
    cursor = parent.started_at
    planned: list[tuple[datetime, datetime, str | None, str | None, tuple[str, ...]]] = []

    for spec in specs:
        if (spec.duration is None) == (spec.ended_at is None):
            raise LedgerValidationError(
                "Each IntervalSlice must specify exactly one of duration or ended_at."
            )
        act_id = (
            None if spec.activity_id is None else require_active_activity(conn, spec.activity_id).id
        )
        slice_end = _resolve_slice_end(cursor, spec)
        slice_notes = parent.notes if spec.notes is None else spec.notes
        slice_tags = parent.tags if spec.tags is None else normalize_tags(spec.tags)
        planned.append((cursor, slice_end, act_id, slice_notes, slice_tags))
        cursor = slice_end

    if cursor != parent.ended_at:
        raise LedgerValidationError(
            "Split slices must sum to the exact duration of the original Interval."
        )
    return planned


def _resolve_slice_end(cursor: datetime, spec: IntervalSlice) -> datetime:
    """Compute and validate the end timestamp for a single `IntervalSlice`."""
    if spec.duration is not None:
        if spec.duration <= timedelta(0):
            raise LedgerValidationError("IntervalSlice duration must be > 0.")
        return cursor + spec.duration
    assert spec.ended_at is not None
    slice_end = ensure_utc(spec.ended_at, field_name="slice.ended_at")
    if slice_end <= cursor:
        raise LedgerValidationError("IntervalSlice ended_at must advance cursor.")
    return slice_end


# endregion
