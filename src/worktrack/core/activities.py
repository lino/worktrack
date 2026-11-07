"""Targeted and Standing `Activity` persistence and freeform tag queries."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from worktrack.core.exceptions import (
    ActivityNotFoundError,
    DuplicateKeyError,
    LedgerValidationError,
)
from worktrack.core.models import (
    ACTIVITY_KIND_TARGETED,
    ALLOWED_ACTIVITY_KINDS,
    DEFAULT_ACTIVITY_SOURCE,
    MAX_IDENTIFIER_LENGTH,
    UNSET,
    Activity,
    UnsetType,
    format_utc_iso,
    normalize_tags,
    parse_utc_iso,
    validate_non_empty_text,
)
from worktrack.core.schema import transaction


# region Activities
def create_activity(
    conn: sqlite3.Connection,
    name: str,
    *,
    kind: str = ACTIVITY_KIND_TARGETED,
    external_ref: str | None = None,
    rollup_target_ref: str | None = None,
    source: str = DEFAULT_ACTIVITY_SOURCE,
    tags: Sequence[str] = (),
    archived: bool = False,
    activity_id: str | None = None,
) -> Activity:
    """Persist a *Targeted* or *Standing* `Activity` and its freeform tags."""
    if kind not in ALLOWED_ACTIVITY_KINDS:
        allowed = ", ".join(sorted(ALLOWED_ACTIVITY_KINDS))
        raise LedgerValidationError(f"Activity kind must be one of: {allowed}.")
    clean_name = validate_non_empty_text(name, field_name="Activity name")
    clean_source = validate_non_empty_text(
        source, field_name="Activity source", max_len=MAX_IDENTIFIER_LENGTH
    )
    clean_ext = _clean_optional_ref(external_ref, field_name="external_ref")
    clean_rollup = _clean_optional_ref(rollup_target_ref, field_name="rollup_target_ref")
    clean_tags = normalize_tags(tags)
    resolved_id = (
        str(uuid.uuid4())
        if activity_id is None
        else validate_non_empty_text(
            activity_id, field_name="activity_id", max_len=MAX_IDENTIFIER_LENGTH
        )
    )
    now_iso = format_utc_iso(datetime.now(UTC))

    try:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO activities
                    (id, name, kind, external_ref, rollup_target_ref, source, archived,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_id,
                    clean_name,
                    kind,
                    clean_ext,
                    clean_rollup,
                    clean_source,
                    int(archived),
                    now_iso,
                    now_iso,
                ),
            )
            _replace_activity_tags(conn, resolved_id, clean_tags)
    except sqlite3.IntegrityError as exc:
        raise DuplicateKeyError(f"Activity '{resolved_id}' already exists.") from exc
    return get_activity(conn, resolved_id)


def get_activity(conn: sqlite3.Connection, activity_id: str) -> Activity:
    """Fetch a single `Activity` by identifier."""
    row = conn.execute(
        """
        SELECT id, name, kind, external_ref, rollup_target_ref, source, archived,
               created_at, updated_at
        FROM activities
        WHERE id = ?
        """,
        (activity_id.strip(),),
    ).fetchone()
    if row is None:
        raise ActivityNotFoundError(f"Activity '{activity_id}' does not exist.")
    tags_map = _load_activity_tags(conn, [str(row["id"])])
    return _row_to_activity(row, tags_map.get(str(row["id"]), ()))


def require_active_activity(conn: sqlite3.Connection, activity_id: str) -> Activity:
    """Verify an `Activity` exists and is not archived before recording work."""
    activity = get_activity(conn, activity_id)
    if activity.archived:
        raise LedgerValidationError(f"Activity '{activity.id}' ('{activity.name}') is archived.")
    return activity


def find_activity_by_external_ref(
    conn: sqlite3.Connection,
    external_ref: str,
    *,
    source: str | None = None,
) -> Activity | None:
    """Look up an `Activity` by external reference (e.g. Jira key `PROJ-142`)."""
    clean_ref = external_ref.strip()
    if source is not None:
        row = conn.execute(
            """
            SELECT id, name, kind, external_ref, rollup_target_ref, source, archived,
                   created_at, updated_at
            FROM activities
            WHERE external_ref = ? AND source = ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (clean_ref, source.strip()),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id, name, kind, external_ref, rollup_target_ref, source, archived,
                   created_at, updated_at
            FROM activities
            WHERE external_ref = ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (clean_ref,),
        ).fetchone()
    if row is None:
        return None
    tags_map = _load_activity_tags(conn, [str(row["id"])])
    return _row_to_activity(row, tags_map.get(str(row["id"]), ()))


def update_activity(
    conn: sqlite3.Connection,
    activity_id: str,
    *,
    name: str | None = None,
    external_ref: str | UnsetType | None = UNSET,
    rollup_target_ref: str | UnsetType | None = UNSET,
    archived: bool | None = None,
    tags: Sequence[str] | None = None,
) -> Activity:
    """Update mutable `Activity` attributes and optional tag set atomically."""
    with transaction(conn):
        existing = get_activity(conn, activity_id)
        next_name = (
            existing.name
            if name is None
            else validate_non_empty_text(name, field_name="Activity name")
        )
        next_ext = (
            existing.external_ref
            if external_ref is UNSET
            else _clean_optional_ref(external_ref, field_name="external_ref")
        )
        next_rollup = (
            existing.rollup_target_ref
            if rollup_target_ref is UNSET
            else _clean_optional_ref(rollup_target_ref, field_name="rollup_target_ref")
        )
        next_archived = existing.archived if archived is None else archived
        now_iso = format_utc_iso(datetime.now(UTC))
        conn.execute(
            """
            UPDATE activities
            SET name = ?, external_ref = ?, rollup_target_ref = ?, archived = ?, updated_at = ?
            WHERE id = ?
            """,
            (next_name, next_ext, next_rollup, int(next_archived), now_iso, existing.id),
        )
        if tags is not None:
            _replace_activity_tags(conn, existing.id, normalize_tags(tags))
    return get_activity(conn, existing.id)


def list_activities(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    include_archived: bool = False,
    tag: str | None = None,
    tags: Sequence[str] | None = None,
    source: str | None = None,
) -> list[Activity]:
    """Query `Activities` filtered by kind, archival status, source, and tags."""
    clauses: list[str] = []
    params: list[object] = []
    if kind is not None:
        if kind not in ALLOWED_ACTIVITY_KINDS:
            raise LedgerValidationError(f"Unknown Activity kind '{kind}'.")
        clauses.append("kind = ?")
        params.append(kind)
    if not include_archived:
        clauses.append("archived = 0")
    if source is not None:
        clauses.append("source = ?")
        params.append(source.strip())
    for required_tag in _collect_tag_filters(tag, tags):
        clauses.append(
            "EXISTS (SELECT 1 FROM activity_tags t "
            "WHERE t.activity_id = activities.id AND t.tag = ?)"
        )
        params.append(required_tag)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT id, name, kind, external_ref, rollup_target_ref, source, archived,
               created_at, updated_at
        FROM activities
        {where_sql}
        ORDER BY created_at ASC, id ASC
        """,
        params,
    ).fetchall()
    tags_map = _load_activity_tags(conn, [str(row["id"]) for row in rows])
    return [_row_to_activity(row, tags_map.get(str(row["id"]), ())) for row in rows]


# endregion


# region Persistence
def _clean_optional_ref(value: str | None, *, field_name: str) -> str | None:
    """Validate an optional external or rollup reference string."""
    if value is None:
        return None
    return validate_non_empty_text(value, field_name=field_name, max_len=MAX_IDENTIFIER_LENGTH)


def _collect_tag_filters(tag: str | None, tags: Sequence[str] | None) -> tuple[str, ...]:
    """Combine singular `tag` and plural `tags` filter parameters."""
    combined: list[str] = []
    if tag is not None:
        combined.append(tag)
    if tags is not None:
        combined.extend(tags)
    return normalize_tags(combined) if combined else ()


def _replace_activity_tags(
    conn: sqlite3.Connection,
    activity_id: str,
    tags: tuple[str, ...],
) -> None:
    """Replace all `activity_tags` rows for `activity_id`."""
    conn.execute("DELETE FROM activity_tags WHERE activity_id = ?", (activity_id,))
    if tags:
        conn.executemany(
            "INSERT INTO activity_tags (activity_id, tag) VALUES (?, ?)",
            [(activity_id, item) for item in tags],
        )


def _load_activity_tags(
    conn: sqlite3.Connection,
    activity_ids: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    """Batch-load sorted tag tuples for a sequence of `Activity` IDs."""
    if not activity_ids:
        return {}
    placeholders = ", ".join("?" for _ in activity_ids)
    rows = conn.execute(
        f"""
        SELECT activity_id, tag
        FROM activity_tags
        WHERE activity_id IN ({placeholders})
        ORDER BY tag ASC
        """,
        list(activity_ids),
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["activity_id"]), []).append(str(row["tag"]))
    return {act_id: tuple(items) for act_id, items in grouped.items()}


def _row_to_activity(row: sqlite3.Row, tags: tuple[str, ...]) -> Activity:
    """Hydrate an `Activity` dataclass from a SQLite row and its tags."""
    raw_ext = row["external_ref"]
    raw_rollup = row["rollup_target_ref"]
    return Activity(
        id=str(row["id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        external_ref=None if raw_ext is None else str(raw_ext),
        rollup_target_ref=None if raw_rollup is None else str(raw_rollup),
        source=str(row["source"]),
        archived=bool(row["archived"]),
        tags=tags,
        created_at=parse_utc_iso(str(row["created_at"])),
        updated_at=parse_utc_iso(str(row["updated_at"])),
    )


# endregion
