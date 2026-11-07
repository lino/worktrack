"""Non-strict `Entity` CRUD, `Attribution` edge persistence, and cycle guards."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from worktrack.core.activities import get_activity
from worktrack.core.exceptions import (
    AttributionCycleError,
    DuplicateKeyError,
    EntityNotFoundError,
    InvalidAttributionWeightError,
    LedgerValidationError,
    NotFoundError,
)
from worktrack.core.models import (
    MAX_ATTRIBUTION_WEIGHT,
    MAX_IDENTIFIER_LENGTH,
    WEIGHT_SUM_TOLERANCE,
    Attribution,
    Entity,
    format_utc_iso,
    parse_utc_iso,
    validate_attribution_weight,
    validate_entity_key,
    validate_lower_slug,
    validate_non_empty_text,
)
from worktrack.core.schema import transaction

# region Constants
ZERO_WEIGHT: Final = 0.0
# endregion


# region Entities
def create_entity(
    conn: sqlite3.Connection,
    kind: str,
    key: str,
    name: str,
    *,
    metadata: Mapping[str, object] | None = None,
    entity_id: str | None = None,
) -> Entity:
    """Persist a non-strict `Entity` node (`project`, `component`, `customer`, etc.)."""
    clean_kind = validate_lower_slug(kind, field_name="Entity kind")
    clean_key = validate_entity_key(key)
    clean_name = validate_non_empty_text(name, field_name="Entity name")
    resolved_id = (
        str(uuid.uuid4())
        if entity_id is None
        else validate_non_empty_text(
            entity_id, field_name="entity_id", max_len=MAX_IDENTIFIER_LENGTH
        )
    )
    meta_dict = {} if metadata is None else dict(metadata)
    encoded_meta = _encode_metadata(meta_dict)
    now_iso = format_utc_iso(datetime.now(UTC))

    try:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO entities
                    (id, kind, key, name, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (resolved_id, clean_kind, clean_key, clean_name, encoded_meta, now_iso, now_iso),
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateKeyError(
            f"Entity ({clean_kind}, {clean_key}) or id '{resolved_id}' already exists."
        ) from exc
    return get_entity(conn, resolved_id)


def _encode_metadata(metadata: dict[str, object]) -> str:
    """Serialize `Entity.metadata` to JSON or raise `LedgerValidationError`."""
    try:
        return json.dumps(metadata, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError(f"Entity metadata must be JSON-serializable: {exc}") from exc


def get_entity(conn: sqlite3.Connection, entity_id: str) -> Entity:
    """Fetch a single `Entity` by identifier."""
    row = conn.execute(
        """
        SELECT id, kind, key, name, metadata_json, created_at, updated_at
        FROM entities
        WHERE id = ?
        """,
        (entity_id.strip(),),
    ).fetchone()
    if row is None:
        raise EntityNotFoundError(f"Entity '{entity_id}' does not exist.")
    return _row_to_entity(row)


def find_entity(conn: sqlite3.Connection, kind: str, key: str) -> Entity | None:
    """Look up an `Entity` by `(kind, key)` pair."""
    row = conn.execute(
        """
        SELECT id, kind, key, name, metadata_json, created_at, updated_at
        FROM entities
        WHERE kind = ? AND key = ?
        """,
        (kind.strip(), key.strip()),
    ).fetchone()
    return None if row is None else _row_to_entity(row)


def list_entities(conn: sqlite3.Connection, *, kind: str | None = None) -> list[Entity]:
    """List `Entities` optionally filtered by `kind`."""
    if kind is not None:
        clean_kind = validate_lower_slug(kind, field_name="Entity kind")
        rows = conn.execute(
            """
            SELECT id, kind, key, name, metadata_json, created_at, updated_at
            FROM entities
            WHERE kind = ?
            ORDER BY kind ASC, key ASC
            """,
            (clean_kind,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, kind, key, name, metadata_json, created_at, updated_at
            FROM entities
            ORDER BY kind ASC, key ASC
            """
        ).fetchall()
    return [_row_to_entity(row) for row in rows]


def _row_to_entity(row: sqlite3.Row) -> Entity:
    """Hydrate an `Entity` dataclass from a SQLite row."""
    parsed_meta = json.loads(str(row["metadata_json"]))
    return Entity(
        id=str(row["id"]),
        kind=str(row["kind"]),
        key=str(row["key"]),
        name=str(row["name"]),
        metadata=dict(parsed_meta),
        created_at=parse_utc_iso(str(row["created_at"])),
        updated_at=parse_utc_iso(str(row["updated_at"])),
    )


# endregion


# region Attribution
def set_attribution(
    conn: sqlite3.Connection,
    *,
    target_entity_id: str,
    weight: float,
    source_activity_id: str | None = None,
    source_entity_id: str | None = None,
    enforce_weight_sum: bool = False,
) -> Attribution:
    """Upsert a weighted edge (`Activity -> Entity` or cycle-free `Entity -> Entity`)."""
    numeric_weight = validate_attribution_weight(weight)
    clean_act, clean_ent = validate_exclusive_source(source_activity_id, source_entity_id)
    clean_target = target_entity_id.strip()
    now_iso = format_utc_iso(datetime.now(UTC))

    with transaction(conn):
        target_entity = get_entity(conn, clean_target)
        if clean_act is not None:
            get_activity(conn, clean_act)
        else:
            assert clean_ent is not None
            get_entity(conn, clean_ent)
            _assert_no_entity_cycle(
                conn, source_entity_id=clean_ent, target_entity_id=target_entity.id
            )
        if enforce_weight_sum:
            _assert_outgoing_sum_within_unit(
                conn,
                source_activity_id=clean_act,
                source_entity_id=clean_ent,
                target_entity=target_entity,
                candidate_weight=numeric_weight,
            )
        edge_id = _upsert_attribution_row(
            conn,
            source_activity_id=clean_act,
            source_entity_id=clean_ent,
            target_entity_id=target_entity.id,
            weight=numeric_weight,
            now_iso=now_iso,
        )
    return _get_attribution(conn, edge_id)


def remove_attribution(conn: sqlite3.Connection, attribution_id: str) -> None:
    """Delete an `Attribution` edge by identifier."""
    with transaction(conn):
        cursor = conn.execute("DELETE FROM attributions WHERE id = ?", (attribution_id.strip(),))
        if cursor.rowcount == 0:
            raise NotFoundError(f"Attribution '{attribution_id}' does not exist.")


def list_attributions(
    conn: sqlite3.Connection,
    *,
    source_activity_id: str | None = None,
    source_entity_id: str | None = None,
    target_entity_id: str | None = None,
) -> list[Attribution]:
    """Query `Attribution` edges filtered by optional source or target identifiers."""
    clauses: list[str] = []
    params: list[object] = []
    if source_activity_id is not None:
        clauses.append("source_activity_id = ?")
        params.append(source_activity_id.strip())
    if source_entity_id is not None:
        clauses.append("source_entity_id = ?")
        params.append(source_entity_id.strip())
    if target_entity_id is not None:
        clauses.append("target_entity_id = ?")
        params.append(target_entity_id.strip())
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT id, source_activity_id, source_entity_id, target_entity_id, weight,
               created_at, updated_at
        FROM attributions
        {where_sql}
        ORDER BY created_at ASC, id ASC
        """,
        params,
    ).fetchall()
    return [_row_to_attribution(row) for row in rows]


def validate_outgoing_weights(
    conn: sqlite3.Connection,
    *,
    source_activity_id: str | None = None,
    source_entity_id: str | None = None,
) -> dict[str, float]:
    """Return outgoing `Attribution` weight sums grouped by target `Entity.kind`."""
    clean_act, clean_ent = validate_exclusive_source(source_activity_id, source_entity_id)
    col_name = "source_activity_id" if clean_act is not None else "source_entity_id"
    source_id = clean_act if clean_act is not None else clean_ent
    rows = conn.execute(
        f"""
        SELECT e.kind AS target_kind, SUM(a.weight) AS total_weight
        FROM attributions a
        JOIN entities e ON e.id = a.target_entity_id
        WHERE a.{col_name} = ?
        GROUP BY e.kind
        """,
        (source_id,),
    ).fetchall()
    return {str(row["target_kind"]): float(row["total_weight"]) for row in rows}


# endregion


# region Validation
def validate_exclusive_source(
    source_activity_id: str | None,
    source_entity_id: str | None,
) -> tuple[str | None, str | None]:
    """Verify exactly one of `source_activity_id` or `source_entity_id` is passed."""
    has_act = source_activity_id is not None
    has_ent = source_entity_id is not None
    if has_act == has_ent:
        raise LedgerValidationError(
            "Specify exactly one of source_activity_id or source_entity_id."
        )
    if source_activity_id is not None:
        return source_activity_id.strip(), None
    assert source_entity_id is not None
    return None, source_entity_id.strip()


def _assert_no_entity_cycle(
    conn: sqlite3.Connection,
    *,
    source_entity_id: str,
    target_entity_id: str,
) -> None:
    """Reject self-loops and multi-hop cycles in `Entity -> Entity` edges via BFS."""
    if source_entity_id == target_entity_id:
        raise AttributionCycleError("Self-referential Entity Attribution is forbidden.")
    rows = conn.execute(
        """
        SELECT source_entity_id, target_entity_id
        FROM attributions
        WHERE source_entity_id IS NOT NULL
        """
    ).fetchall()
    adjacency: dict[str, list[str]] = {}
    for row in rows:
        adjacency.setdefault(str(row["source_entity_id"]), []).append(str(row["target_entity_id"]))
    queue: deque[str] = deque([target_entity_id])
    visited: set[str] = {target_entity_id}
    while queue:
        current = queue.popleft()
        for neighbor in adjacency.get(current, ()):
            if neighbor == source_entity_id:
                raise AttributionCycleError(
                    f"Adding edge {source_entity_id} -> {target_entity_id} creates a cycle."
                )
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)


def _assert_outgoing_sum_within_unit(
    conn: sqlite3.Connection,
    *,
    source_activity_id: str | None,
    source_entity_id: str | None,
    target_entity: Entity,
    candidate_weight: float,
) -> None:
    """Verify outgoing weights to `target_entity.kind` stay `<= 1.0`."""
    col_name = "source_activity_id" if source_activity_id is not None else "source_entity_id"
    source_id = source_activity_id if source_activity_id is not None else source_entity_id
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(a.weight), 0.0) AS other_sum
        FROM attributions a
        JOIN entities e ON e.id = a.target_entity_id
        WHERE a.{col_name} = ? AND e.kind = ? AND a.target_entity_id != ?
        """,
        (source_id, target_entity.kind, target_entity.id),
    ).fetchone()
    other_sum = ZERO_WEIGHT if row is None else float(row["other_sum"])
    if other_sum + candidate_weight > MAX_ATTRIBUTION_WEIGHT + WEIGHT_SUM_TOLERANCE:
        raise InvalidAttributionWeightError(
            f"Outgoing weights for kind '{target_entity.kind}' exceed 1.0 "
            f"({other_sum + candidate_weight:.4f})."
        )


# endregion


# region Persistence
def _upsert_attribution_row(
    conn: sqlite3.Connection,
    *,
    source_activity_id: str | None,
    source_entity_id: str | None,
    target_entity_id: str,
    weight: float,
    now_iso: str,
) -> str:
    """Insert or update an `Attribution` edge and return its identifier."""
    new_id = str(uuid.uuid4())
    if source_activity_id is not None:
        conn.execute(
            """
            INSERT INTO attributions
                (id, source_activity_id, source_entity_id, target_entity_id, weight,
                 created_at, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(source_activity_id, target_entity_id)
                WHERE source_activity_id IS NOT NULL
            DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at
            """,
            (new_id, source_activity_id, target_entity_id, weight, now_iso, now_iso),
        )
        row = conn.execute(
            "SELECT id FROM attributions WHERE source_activity_id = ? AND target_entity_id = ?",
            (source_activity_id, target_entity_id),
        ).fetchone()
    else:
        conn.execute(
            """
            INSERT INTO attributions
                (id, source_activity_id, source_entity_id, target_entity_id, weight,
                 created_at, updated_at)
            VALUES (?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(source_entity_id, target_entity_id)
                WHERE source_entity_id IS NOT NULL
            DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at
            """,
            (new_id, source_entity_id, target_entity_id, weight, now_iso, now_iso),
        )
        row = conn.execute(
            "SELECT id FROM attributions WHERE source_entity_id = ? AND target_entity_id = ?",
            (source_entity_id, target_entity_id),
        ).fetchone()
    assert row is not None
    return str(row["id"])


def _get_attribution(conn: sqlite3.Connection, attribution_id: str) -> Attribution:
    """Fetch a single `Attribution` row by identifier."""
    row = conn.execute(
        """
        SELECT id, source_activity_id, source_entity_id, target_entity_id, weight,
               created_at, updated_at
        FROM attributions
        WHERE id = ?
        """,
        (attribution_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Attribution '{attribution_id}' does not exist.")
    return _row_to_attribution(row)


def _row_to_attribution(row: sqlite3.Row) -> Attribution:
    """Hydrate an `Attribution` dataclass from a SQLite row."""
    raw_act = row["source_activity_id"]
    raw_ent = row["source_entity_id"]
    return Attribution(
        id=str(row["id"]),
        target_entity_id=str(row["target_entity_id"]),
        weight=float(row["weight"]),
        source_activity_id=None if raw_act is None else str(raw_act),
        source_entity_id=None if raw_ent is None else str(raw_ent),
        created_at=parse_utc_iso(str(row["created_at"])),
        updated_at=parse_utc_iso(str(row["updated_at"])),
    )


# endregion
