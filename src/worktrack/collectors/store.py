"""SQLite `ClueStore` for local-only ambient `Clue` persistence and queries."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from worktrack.collectors.models import (
    DEFAULT_CLUE_CONFIDENCE,
    EMPTY_CLUE_SCHEMA_VERSION,
    MAX_EXTERNAL_KEY_LENGTH,
    MAX_SUMMARY_LENGTH,
    Clue,
    ClueNotFoundError,
    ClueValidationError,
    CollectorError,
    validate_clue_confidence,
    validate_clue_text,
    validate_clue_timestamp,
    validate_optional_clue_text,
)
from worktrack.collectors.schema import apply_clue_migrations, list_applied_clue_migrations
from worktrack.config import WorktrackSettings
from worktrack.core.models import (
    MAX_IDENTIFIER_LENGTH,
    MigrationRecord,
    format_utc_iso,
    parse_utc_iso,
)
from worktrack.core.schema import (
    DEFAULT_DB_DIR_MODE,
    open_sqlite_connection,
    prepare_db_target,
    resolve_busy_timeout_ms,
    transaction,
)
from worktrack.telemetry.metrics import TelemetryRecorder


# region Clue Store
class ClueStore:
    """Local-only SQLite store for ambient `Clue` hints with zero external egress."""

    def __init__(
        self,
        db_path: Path | str = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
        telemetry: TelemetryRecorder | None = None,
        busy_timeout_ms: int | None = None,
        dir_mode: int = DEFAULT_DB_DIR_MODE,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Open or attach SQLite WAL connection and apply `clue_schema_migrations`."""
        resolved_timeout = resolve_busy_timeout_ms(busy_timeout_ms, environ=environ)
        if connection is None:
            connect_target, resolved_path = prepare_db_target(db_path, dir_mode=dir_mode)
            conn = open_sqlite_connection(connect_target, busy_timeout_ms=resolved_timeout)
            owns_conn = True
        else:
            conn = connection
            resolved_path = None
            owns_conn = False

        try:
            apply_clue_migrations(conn)
        except Exception:
            if owns_conn:
                conn.close()
            raise

        self._conn = conn
        self._owns_conn = owns_conn
        self._db_path = resolved_path
        self._telemetry = telemetry
        self._closed = False

    @classmethod
    def from_settings(
        cls,
        settings: WorktrackSettings,
        *,
        telemetry: TelemetryRecorder | None = None,
        busy_timeout_ms: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Construct a `ClueStore` bound to `settings.db_path`."""
        return cls(
            settings.db_path,
            telemetry=telemetry,
            busy_timeout_ms=busy_timeout_ms,
            environ=environ,
        )

    @property
    def db_path(self) -> Path | None:
        """Return resolved SQLite file `Path` (or `None` for `:memory:` / shared conn)."""
        return self._db_path

    @property
    def clue_schema_version(self) -> int:
        """Return highest applied `clue_schema_migrations` version."""
        row = (
            self._require_conn()
            .execute("SELECT COALESCE(MAX(version), 0) FROM clue_schema_migrations")
            .fetchone()
        )
        return EMPTY_CLUE_SCHEMA_VERSION if row is None else int(row[0])

    @property
    def schema_version(self) -> int:
        """Alias for `clue_schema_version`."""
        return self.clue_schema_version

    def applied_migrations(self) -> list[MigrationRecord]:
        """Return ordered list of applied `clue_schema_migrations` records."""
        return list_applied_clue_migrations(self._require_conn())

    def applied_clue_migrations(self) -> list[MigrationRecord]:
        """Alias for `applied_migrations`."""
        return self.applied_migrations()

    def close(self) -> None:
        """Close owned SQLite connection idempotently."""
        if not self._closed:
            if self._owns_conn:
                self._conn.close()
            self._closed = True

    def __enter__(self) -> Self:
        """Enter context manager returning this `ClueStore`."""
        self._require_conn()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close owned SQLite connection on context exit."""
        self.close()

    def upsert_clue(
        self,
        *,
        source: str,
        external_key: str,
        occurred_at: datetime,
        summary: str,
        inferred_activity_key: str | None = None,
        activity_id: str | None = None,
        confidence: float = DEFAULT_CLUE_CONFIDENCE,
        metadata: Mapping[str, object] | None = None,
        clue_id: str | None = None,
    ) -> Clue:
        """Idempotently insert or update a `Clue` keyed by `(source, external_key)`."""
        clean_source = validate_clue_text(
            source, field_name="source", max_len=MAX_IDENTIFIER_LENGTH
        )
        clean_ext_key = validate_clue_text(
            external_key, field_name="external_key", max_len=MAX_EXTERNAL_KEY_LENGTH
        )
        utc_occurred = validate_clue_timestamp(occurred_at, field_name="occurred_at")
        clean_summary = validate_clue_text(
            summary, field_name="summary", max_len=MAX_SUMMARY_LENGTH
        )
        clean_inferred = validate_optional_clue_text(
            inferred_activity_key, field_name="inferred_activity_key"
        )
        clean_conf = validate_clue_confidence(confidence)
        meta_dict, clean_act_id = _prepare_metadata_and_activity_id(metadata, activity_id)
        candidate_id = (
            validate_clue_text(clue_id, field_name="clue_id", max_len=MAX_IDENTIFIER_LENGTH)
            if clue_id is not None
            else str(uuid.uuid4())
        )
        conn = self._require_conn()
        with transaction(conn):
            _execute_upsert_clue_sql(
                conn,
                (
                    candidate_id,
                    clean_source,
                    clean_ext_key,
                    format_utc_iso(utc_occurred),
                    clean_summary,
                    clean_inferred,
                    clean_act_id,
                    clean_conf,
                    json.dumps(meta_dict, sort_keys=True),
                    format_utc_iso(datetime.now(UTC)),
                ),
            )
            found = self.find_clue(clean_source, clean_ext_key)
        assert found is not None
        return found

    record_clue = upsert_clue

    def get_clue(self, clue_id: str) -> Clue:
        """Fetch a `Clue` by primary identifier or raise `ClueNotFoundError`."""
        clean_id = validate_clue_text(clue_id, field_name="clue_id", max_len=MAX_IDENTIFIER_LENGTH)
        row = (
            self._require_conn()
            .execute(
                "SELECT * FROM clues WHERE id = ?",
                (clean_id,),
            )
            .fetchone()
        )
        if row is None:
            raise ClueNotFoundError(f"Clue '{clean_id}' does not exist.")
        return _row_to_clue(row)

    def find_clue(self, source: str, external_key: str) -> Clue | None:
        """Look up a `Clue` by `(source, external_key)`."""
        row = (
            self._require_conn()
            .execute(
                "SELECT * FROM clues WHERE source = ? AND external_key = ?",
                (source.strip(), external_key.strip()),
            )
            .fetchone()
        )
        return None if row is None else _row_to_clue(row)

    def list_clues(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        source: str | None = None,
        inferred_activity_key: str | None = None,
        activity_id: str | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
    ) -> list[Clue]:
        """Query `Clues` matching optional `[since, until)` and attribute filters."""
        clauses, params = _build_clue_query_filters(
            since=since,
            until=until,
            source=source,
            inferred_activity_key=inferred_activity_key,
            activity_id=activity_id,
            min_confidence=min_confidence,
        )
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = ""
        if limit is not None:
            if isinstance(limit, bool) or limit <= 0:
                raise ClueValidationError("limit must be a positive integer.")
            limit_sql = "LIMIT ?"
            params.append(limit)
        rows = (
            self._require_conn()
            .execute(
                f"SELECT * FROM clues {where_sql} ORDER BY occurred_at ASC, id ASC {limit_sql}",
                params,
            )
            .fetchall()
        )
        return [_row_to_clue(row) for row in rows]

    def delete_clue(self, clue_id: str) -> bool:
        """Delete a `Clue` by primary identifier, returning `True` if removed."""
        clean_id = validate_clue_text(clue_id, field_name="clue_id", max_len=MAX_IDENTIFIER_LENGTH)
        conn = self._require_conn()
        with transaction(conn):
            cursor = conn.execute("DELETE FROM clues WHERE id = ?", (clean_id,))
        return cursor.rowcount > 0

    def _require_conn(self) -> sqlite3.Connection:
        """Return open SQLite connection or raise `CollectorError`."""
        if self._closed:
            raise CollectorError("ClueStore SQLite connection is closed.")
        return self._conn


# endregion


# region Persistence
def _execute_upsert_clue_sql(
    conn: sqlite3.Connection,
    params: tuple[object, ...],
) -> None:
    """Execute SQLite `INSERT ... ON CONFLICT(source, external_key) DO UPDATE` for a `Clue`."""
    conn.execute(
        """
        INSERT INTO clues (
            id, source, external_key, occurred_at, summary,
            inferred_activity_key, activity_id, confidence, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, external_key) DO UPDATE SET
            occurred_at = excluded.occurred_at,
            summary = excluded.summary,
            inferred_activity_key = excluded.inferred_activity_key,
            activity_id = COALESCE(excluded.activity_id, clues.activity_id),
            confidence = excluded.confidence,
            metadata_json = excluded.metadata_json
        """,
        params,
    )


def _prepare_metadata_and_activity_id(
    metadata: Mapping[str, object] | None,
    activity_id: str | None,
) -> tuple[dict[str, object], str | None]:
    """Normalize `metadata` dictionary and synchronize optional `activity_id`."""
    meta_dict: dict[str, object] = {} if metadata is None else dict(metadata)
    clean_act_id = validate_optional_clue_text(activity_id, field_name="activity_id")
    if clean_act_id is None:
        raw_meta_act = meta_dict.get("activity_id")
        if isinstance(raw_meta_act, str) and raw_meta_act.strip():
            clean_act_id = raw_meta_act.strip()
    if clean_act_id is not None:
        meta_dict["activity_id"] = clean_act_id
    return meta_dict, clean_act_id


def _build_clue_query_filters(
    *,
    since: datetime | None,
    until: datetime | None,
    source: str | None,
    inferred_activity_key: str | None,
    activity_id: str | None,
    min_confidence: float | None,
) -> tuple[list[str], list[object]]:
    """Construct parameterized SQL `WHERE` clauses for `ClueStore.list_clues`."""
    clauses: list[str] = []
    params: list[object] = []
    _append_time_window_filters(clauses, params, since=since, until=until)
    _append_attribute_filters(
        clauses,
        params,
        source=source,
        inferred_activity_key=inferred_activity_key,
        activity_id=activity_id,
        min_confidence=min_confidence,
    )
    return clauses, params


def _append_time_window_filters(
    clauses: list[str],
    params: list[object],
    *,
    since: datetime | None,
    until: datetime | None,
) -> None:
    """Validate and append `[since, until)` SQL predicates."""
    utc_since = None if since is None else validate_clue_timestamp(since, field_name="since")
    utc_until = None if until is None else validate_clue_timestamp(until, field_name="until")
    if utc_since is not None and utc_until is not None and utc_until <= utc_since:
        raise ClueValidationError("until must be strictly greater than since.")
    if utc_since is not None:
        clauses.append("occurred_at >= ?")
        params.append(format_utc_iso(utc_since))
    if utc_until is not None:
        clauses.append("occurred_at < ?")
        params.append(format_utc_iso(utc_until))


def _append_attribute_filters(
    clauses: list[str],
    params: list[object],
    *,
    source: str | None,
    inferred_activity_key: str | None,
    activity_id: str | None,
    min_confidence: float | None,
) -> None:
    """Append equality and confidence SQL predicates."""
    equality_fields = (
        ("source", source),
        ("inferred_activity_key", inferred_activity_key),
        ("activity_id", activity_id),
    )
    for col_name, raw_val in equality_fields:
        if raw_val is not None:
            clauses.append(f"{col_name} = ?")
            params.append(raw_val.strip())
    if min_confidence is not None:
        clauses.append("confidence >= ?")
        params.append(validate_clue_confidence(min_confidence))


def _row_to_clue(row: sqlite3.Row) -> Clue:
    """Hydrate a `Clue` dataclass from a SQLite row."""
    raw_meta = json.loads(str(row["metadata_json"]))
    metadata = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
    raw_inferred = row["inferred_activity_key"]
    raw_act_id = row["activity_id"]
    resolved_act_id = None if raw_act_id is None else str(raw_act_id)
    if resolved_act_id is None and isinstance(metadata.get("activity_id"), str):
        resolved_act_id = str(metadata["activity_id"])
    return Clue(
        id=str(row["id"]),
        source=str(row["source"]),
        external_key=str(row["external_key"]),
        occurred_at=parse_utc_iso(str(row["occurred_at"])),
        summary=str(row["summary"]),
        inferred_activity_key=None if raw_inferred is None else str(raw_inferred),
        confidence=float(row["confidence"]),
        metadata=metadata,
        created_at=parse_utc_iso(str(row["created_at"])),
        activity_id=resolved_act_id,
    )


# endregion
