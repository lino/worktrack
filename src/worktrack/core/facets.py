"""Configurable `Facet` schema registration and scoped `ContextFact` storage."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime

from worktrack.core.exceptions import (
    DuplicateKeyError,
    FacetNotFoundError,
    FacetValueValidationError,
    IntervalNotFoundError,
    LedgerValidationError,
)
from worktrack.core.models import (
    ALLOWED_FACET_VALUE_TYPES,
    DEFAULT_CONTEXT_FACT_SOURCE,
    FACET_TYPE_BOOL,
    FACET_TYPE_ENUM,
    FACET_TYPE_FLOAT,
    FACET_TYPE_INT,
    FACET_TYPE_STRING,
    MAX_IDENTIFIER_LENGTH,
    ContextFact,
    Facet,
    FacetValue,
    format_utc_iso,
    normalize_scope_date,
    parse_utc_iso,
    validate_lower_slug,
    validate_non_empty_text,
)
from worktrack.core.schema import transaction


# region Validation
def _validate_allowed_values(value_type: str, allowed_values: Sequence[str]) -> list[str]:
    """Validate enum allowlist vs. scalar `Facet` types."""
    if value_type != FACET_TYPE_ENUM:
        if allowed_values:
            raise LedgerValidationError(
                f"allowed_values is only valid for '{FACET_TYPE_ENUM}' facets."
            )
        return []
    if not allowed_values:
        raise LedgerValidationError(
            f"Facet of type '{FACET_TYPE_ENUM}' requires at least one allowed value."
        )
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in allowed_values:
        cleaned = validate_non_empty_text(
            raw, field_name="allowed_values entry", max_len=MAX_IDENTIFIER_LENGTH
        )
        if cleaned not in seen:
            seen.add(cleaned)
            deduped.append(cleaned)
    return deduped


def _validate_bool_facet(facet: Facet, value: FacetValue) -> FacetValue:
    """Verify `value` is a `bool` instance."""
    if not isinstance(value, bool):
        raise FacetValueValidationError(f"Facet '{facet.key}' expects a bool.")
    return value


def _validate_int_facet(facet: Facet, value: FacetValue) -> FacetValue:
    """Verify `value` is a non-boolean `int` instance."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise FacetValueValidationError(f"Facet '{facet.key}' expects an int.")
    return value


def _validate_float_facet(facet: Facet, value: FacetValue) -> FacetValue:
    """Verify `value` is a finite non-boolean numeric value."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise FacetValueValidationError(f"Facet '{facet.key}' expects a finite float.")
    return float(value)


def _validate_string_facet(facet: Facet, value: FacetValue) -> FacetValue:
    """Verify `value` is a `str` instance."""
    if not isinstance(value, str):
        raise FacetValueValidationError(f"Facet '{facet.key}' expects a string.")
    return value


def _validate_enum_facet(facet: Facet, value: FacetValue) -> FacetValue:
    """Verify `value` is a `str` belonging to `facet.allowed_values`."""
    if not isinstance(value, str) or value not in facet.allowed_values:
        raise FacetValueValidationError(
            f"Facet '{facet.key}' expects one of {facet.allowed_values}, got {value!r}."
        )
    return value


_FACET_VALIDATORS = {
    FACET_TYPE_BOOL: _validate_bool_facet,
    FACET_TYPE_INT: _validate_int_facet,
    FACET_TYPE_FLOAT: _validate_float_facet,
    FACET_TYPE_STRING: _validate_string_facet,
    FACET_TYPE_ENUM: _validate_enum_facet,
}


def validate_facet_value(facet: Facet, value: FacetValue) -> FacetValue:
    """Validate a candidate `ContextFact` value against its `Facet` schema."""
    validator = _FACET_VALIDATORS.get(facet.value_type)
    if validator is None:
        raise LedgerValidationError(f"Unsupported Facet type '{facet.value_type}'.")
    return validator(facet, value)


def _normalize_fact_scope(
    scope_date: str | date | None,
    interval_id: str | None,
) -> tuple[str | None, str | None]:
    """Validate exclusive `scope_date` XOR `interval_id` scoping."""
    has_date = scope_date is not None
    has_interval = interval_id is not None
    if has_date == has_interval:
        raise LedgerValidationError("Exactly one of scope_date or interval_id must be specified.")
    if scope_date is not None:
        return normalize_scope_date(scope_date), None
    assert interval_id is not None
    clean_interval = validate_non_empty_text(
        interval_id, field_name="interval_id", max_len=MAX_IDENTIFIER_LENGTH
    )
    return None, clean_interval


# endregion


# region Facets
def register_facet(
    conn: sqlite3.Connection,
    key: str,
    value_type: str,
    *,
    allowed_values: Sequence[str] = (),
    description: str = "",
) -> Facet:
    """Register a custom typed `Facet` schema in SQLite."""
    clean_key = validate_lower_slug(key, field_name="Facet key")
    if value_type not in ALLOWED_FACET_VALUE_TYPES:
        allowed = ", ".join(sorted(ALLOWED_FACET_VALUE_TYPES))
        raise LedgerValidationError(f"Facet value_type must be one of: {allowed}.")
    clean_allowed = _validate_allowed_values(value_type, allowed_values)
    clean_desc = description.strip()
    now_iso = format_utc_iso(datetime.now(UTC))

    try:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO facets
                    (key, value_type, allowed_values_json, description, builtin, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (clean_key, value_type, json.dumps(clean_allowed), clean_desc, now_iso),
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateKeyError(f"Facet '{clean_key}' is already registered.") from exc
    return get_facet(conn, clean_key)


def get_facet(conn: sqlite3.Connection, key: str) -> Facet:
    """Fetch a registered `Facet` schema by key."""
    clean_key = key.strip()
    row = conn.execute(
        """
        SELECT key, value_type, allowed_values_json, description, builtin, created_at
        FROM facets
        WHERE key = ?
        """,
        (clean_key,),
    ).fetchone()
    if row is None:
        raise FacetNotFoundError(f"Facet '{clean_key}' is not registered.")
    return _row_to_facet(row)


def list_facets(conn: sqlite3.Connection) -> list[Facet]:
    """List all registered `Facet` schemas ordered by key."""
    rows = conn.execute(
        """
        SELECT key, value_type, allowed_values_json, description, builtin, created_at
        FROM facets
        ORDER BY key ASC
        """
    ).fetchall()
    return [_row_to_facet(row) for row in rows]


def _row_to_facet(row: sqlite3.Row) -> Facet:
    """Hydrate a `Facet` dataclass from a SQLite row."""
    parsed_allowed = json.loads(str(row["allowed_values_json"]))
    return Facet(
        key=str(row["key"]),
        value_type=str(row["value_type"]),
        allowed_values=tuple(str(item) for item in parsed_allowed),
        description=str(row["description"]),
        builtin=bool(row["builtin"]),
        created_at=parse_utc_iso(str(row["created_at"])),
    )


# endregion


# region Persistence
def assert_context_fact(
    conn: sqlite3.Connection,
    facet_key: str,
    value: FacetValue,
    *,
    scope_date: str | date | None = None,
    interval_id: str | None = None,
    source: str = DEFAULT_CONTEXT_FACT_SOURCE,
) -> ContextFact:
    """Validate and upsert a date-scoped or interval-scoped `ContextFact`."""
    clean_date, clean_interval = _normalize_fact_scope(scope_date, interval_id)
    clean_source = validate_non_empty_text(
        source, field_name="source", max_len=MAX_IDENTIFIER_LENGTH
    )
    facet = get_facet(conn, facet_key)
    validated_val = validate_facet_value(facet, value)
    encoded_val = json.dumps(validated_val)
    now_iso = format_utc_iso(datetime.now(UTC))
    fact_id = str(uuid.uuid4())

    with transaction(conn):
        if clean_interval is not None:
            _require_interval_exists(conn, clean_interval)
            _upsert_interval_fact(
                conn, fact_id, facet.key, encoded_val, clean_interval, clean_source, now_iso
            )
        else:
            assert clean_date is not None
            _upsert_date_fact(
                conn, fact_id, facet.key, encoded_val, clean_date, clean_source, now_iso
            )
        fetched = get_context_fact(
            conn, facet.key, scope_date=clean_date, interval_id=clean_interval
        )
    assert fetched is not None
    return fetched


def _require_interval_exists(conn: sqlite3.Connection, interval_id: str) -> None:
    """Verify target `Interval` exists before attaching an interval-scoped fact."""
    row = conn.execute("SELECT 1 FROM intervals WHERE id = ?", (interval_id,)).fetchone()
    if row is None:
        raise IntervalNotFoundError(f"Interval '{interval_id}' does not exist.")


def _upsert_date_fact(
    conn: sqlite3.Connection,
    fact_id: str,
    facet_key: str,
    encoded_val: str,
    scope_date: str,
    source: str,
    now_iso: str,
) -> None:
    """Upsert a date-scoped `ContextFact` row."""
    conn.execute(
        """
        INSERT INTO context_facts
            (id, facet_key, value_json, scope_date, interval_id, source, updated_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(facet_key, scope_date) WHERE scope_date IS NOT NULL
        DO UPDATE SET
            value_json = excluded.value_json,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (fact_id, facet_key, encoded_val, scope_date, source, now_iso),
    )


def _upsert_interval_fact(
    conn: sqlite3.Connection,
    fact_id: str,
    facet_key: str,
    encoded_val: str,
    interval_id: str,
    source: str,
    now_iso: str,
) -> None:
    """Upsert an interval-scoped `ContextFact` row."""
    conn.execute(
        """
        INSERT INTO context_facts
            (id, facet_key, value_json, scope_date, interval_id, source, updated_at)
        VALUES (?, ?, ?, NULL, ?, ?, ?)
        ON CONFLICT(facet_key, interval_id) WHERE interval_id IS NOT NULL
        DO UPDATE SET
            value_json = excluded.value_json,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (fact_id, facet_key, encoded_val, interval_id, source, now_iso),
    )


def get_context_fact(
    conn: sqlite3.Connection,
    facet_key: str,
    *,
    scope_date: str | date | None = None,
    interval_id: str | None = None,
) -> ContextFact | None:
    """Look up a single `ContextFact` by `facet_key` and exclusive scope."""
    clean_date, clean_interval = _normalize_fact_scope(scope_date, interval_id)
    scope_col = "scope_date" if clean_date is not None else "interval_id"
    scope_val = clean_date if clean_date is not None else clean_interval
    row = conn.execute(
        f"""
        SELECT id, facet_key, value_json, scope_date, interval_id, source, updated_at
        FROM context_facts
        WHERE facet_key = ? AND {scope_col} = ?
        """,
        (facet_key.strip(), scope_val),
    ).fetchone()
    return None if row is None else _row_to_context_fact(row)


def _append_date_bounds(
    clauses: list[str],
    params: list[object],
    since_date: str | date | None,
    until_date: str | date | None,
) -> None:
    """Validate and append inclusive `since_date` / `until_date` SQL clauses."""
    norm_since = None
    norm_until = None
    if since_date is not None:
        norm_since = normalize_scope_date(since_date)
        clauses.append("scope_date IS NOT NULL AND scope_date >= ?")
        params.append(norm_since)
    if until_date is not None:
        norm_until = normalize_scope_date(until_date)
        clauses.append("scope_date IS NOT NULL AND scope_date <= ?")
        params.append(norm_until)
    if norm_since is not None and norm_until is not None and norm_until < norm_since:
        raise LedgerValidationError("until_date must be >= since_date.")


def list_context_facts(
    conn: sqlite3.Connection,
    *,
    facet_key: str | None = None,
    since_date: str | date | None = None,
    until_date: str | date | None = None,
    scope_date: str | date | None = None,
    interval_id: str | None = None,
) -> list[ContextFact]:
    """Query `ContextFacts` filtered by facet key, date bounds, or interval ID."""
    clauses: list[str] = []
    params: list[object] = []
    if facet_key is not None:
        clauses.append("facet_key = ?")
        params.append(facet_key.strip())
    if scope_date is not None:
        clauses.append("scope_date = ?")
        params.append(normalize_scope_date(scope_date))
    _append_date_bounds(clauses, params, since_date, until_date)
    if interval_id is not None:
        clauses.append("interval_id = ?")
        params.append(interval_id.strip())

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT id, facet_key, value_json, scope_date, interval_id, source, updated_at
        FROM context_facts
        {where_sql}
        ORDER BY scope_date ASC, facet_key ASC, updated_at ASC
        """,
        params,
    ).fetchall()
    return [_row_to_context_fact(row) for row in rows]


def clone_interval_context_facts(
    conn: sqlite3.Connection,
    source_interval_id: str,
    target_interval_ids: Sequence[str],
) -> None:
    """Copy interval-scoped `ContextFacts` from a split parent onto child slices."""
    rows = conn.execute(
        """
        SELECT facet_key, value_json, source, updated_at
        FROM context_facts
        WHERE interval_id = ?
        """,
        (source_interval_id,),
    ).fetchall()
    if not rows:
        return
    for target_id in target_interval_ids:
        for row in rows:
            _upsert_interval_fact(
                conn,
                str(uuid.uuid4()),
                str(row["facet_key"]),
                str(row["value_json"]),
                target_id,
                str(row["source"]),
                str(row["updated_at"]),
            )


def _row_to_context_fact(row: sqlite3.Row) -> ContextFact:
    """Hydrate a typed `ContextFact` dataclass from a SQLite row."""
    decoded: FacetValue = json.loads(str(row["value_json"]))
    raw_date = row["scope_date"]
    raw_interval = row["interval_id"]
    return ContextFact(
        id=str(row["id"]),
        facet_key=str(row["facet_key"]),
        value=decoded,
        scope_date=None if raw_date is None else str(raw_date),
        interval_id=None if raw_interval is None else str(raw_interval),
        source=str(row["source"]),
        updated_at=parse_utc_iso(str(row["updated_at"])),
    )


# endregion
