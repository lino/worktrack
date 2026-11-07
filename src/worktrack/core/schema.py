"""SQLite WAL connection lifecycle, pragmas, and deterministic migrations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from worktrack.core.exceptions import LedgerValidationError, MigrationError
from worktrack.core.models import (
    BUILTIN_WORKPLACE_DESCRIPTION,
    BUILTIN_WORKPLACE_FACET_KEY,
    BUILTIN_WORKPLACE_VALUES,
    FACET_TYPE_ENUM,
    MigrationRecord,
    format_utc_iso,
    parse_utc_iso,
)

# region Constants
DEFAULT_SQLITE_BUSY_TIMEOUT_MS: Final = 5_000
MIN_SQLITE_BUSY_TIMEOUT_MS: Final = 0
MILLISECONDS_PER_SECOND: Final = 1_000.0
ENV_SQLITE_BUSY_TIMEOUT_MS: Final = "WORKTRACK_SQLITE_BUSY_TIMEOUT_MS"
DEFAULT_DB_DIR_MODE: Final = 0o700
MEMORY_DB_SENTINEL: Final = ":memory:"
SQLITE_SYNCHRONOUS_MODE: Final = "NORMAL"
SCHEMA_VERSION_INITIAL: Final = 1

SCHEMA_MIGRATIONS_BOOTSTRAP_SQL: Final = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

MIGRATION_V1_INITIAL_LEDGER_SQL: Final = """
CREATE TABLE activities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('targeted', 'standing')),
    external_ref TEXT,
    rollup_target_ref TEXT,
    source TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_activities_kind_archived ON activities(kind, archived);
CREATE INDEX idx_activities_external_ref ON activities(external_ref, source);

CREATE TABLE activity_tags (
    activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (activity_id, tag)
);

CREATE INDEX idx_activity_tags_tag ON activity_tags(tag);

CREATE TABLE intervals (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_us INTEGER,
    activity_id TEXT REFERENCES activities(id) ON DELETE RESTRICT,
    capture_mode TEXT NOT NULL CHECK (capture_mode IN ('structured', 'chaotic')),
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (ended_at IS NULL AND duration_us IS NULL)
        OR (ended_at IS NOT NULL AND duration_us IS NOT NULL AND duration_us > 0)
    )
);

CREATE INDEX idx_intervals_open ON intervals(started_at) WHERE ended_at IS NULL;
CREATE INDEX idx_intervals_window ON intervals(started_at, ended_at);
CREATE INDEX idx_intervals_activity ON intervals(activity_id, started_at);

CREATE TABLE interval_tags (
    interval_id TEXT NOT NULL REFERENCES intervals(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (interval_id, tag)
);

CREATE INDEX idx_interval_tags_tag ON interval_tags(tag);

CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, key)
);

CREATE INDEX idx_entities_kind ON entities(kind);

CREATE TABLE attributions (
    id TEXT PRIMARY KEY,
    source_activity_id TEXT REFERENCES activities(id) ON DELETE CASCADE,
    source_entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    weight REAL NOT NULL CHECK (weight > 0.0 AND weight <= 1.0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (source_activity_id IS NOT NULL AND source_entity_id IS NULL)
        OR (source_activity_id IS NULL AND source_entity_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_attributions_activity_target
    ON attributions(source_activity_id, target_entity_id)
    WHERE source_activity_id IS NOT NULL;

CREATE UNIQUE INDEX idx_attributions_entity_target
    ON attributions(source_entity_id, target_entity_id)
    WHERE source_entity_id IS NOT NULL;

CREATE INDEX idx_attributions_target ON attributions(target_entity_id);

CREATE TABLE facets (
    key TEXT PRIMARY KEY,
    value_type TEXT NOT NULL CHECK (
        value_type IN ('enum', 'string', 'int', 'float', 'bool')
    ),
    allowed_values_json TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE context_facts (
    id TEXT PRIMARY KEY,
    facet_key TEXT NOT NULL REFERENCES facets(key) ON DELETE CASCADE,
    value_json TEXT NOT NULL,
    scope_date TEXT,
    interval_id TEXT REFERENCES intervals(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (scope_date IS NOT NULL AND interval_id IS NULL)
        OR (scope_date IS NULL AND interval_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_context_facts_date
    ON context_facts(facet_key, scope_date)
    WHERE scope_date IS NOT NULL;

CREATE UNIQUE INDEX idx_context_facts_interval
    ON context_facts(facet_key, interval_id)
    WHERE interval_id IS NOT NULL;

CREATE INDEX idx_context_facts_scope_date ON context_facts(scope_date);
"""
# endregion


# region Schema
@dataclass(frozen=True, slots=True)
class Migration:
    """Deterministic SQLite schema migration step with SHA-256 integrity hash."""

    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        """Compute deterministic SHA-256 hex digest of migration SQL."""
        normalized = self.sql.strip().encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        version=SCHEMA_VERSION_INITIAL,
        name="0001_initial_domain_ledger",
        sql=MIGRATION_V1_INITIAL_LEDGER_SQL,
    ),
)
# endregion


# region Lifecycle
def resolve_busy_timeout_ms(
    busy_timeout_ms: int | None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Resolve SQLite busy timeout from explicit argument, environment, or default."""
    if busy_timeout_ms is not None:
        resolved = busy_timeout_ms
    else:
        env = os.environ if environ is None else environ
        raw_env = env.get(ENV_SQLITE_BUSY_TIMEOUT_MS, "").strip()
        if raw_env:
            try:
                resolved = int(raw_env)
            except ValueError as exc:
                raise LedgerValidationError(
                    f"{ENV_SQLITE_BUSY_TIMEOUT_MS} must be an integer, got '{raw_env}'."
                ) from exc
        else:
            resolved = DEFAULT_SQLITE_BUSY_TIMEOUT_MS
    if isinstance(resolved, bool) or resolved < MIN_SQLITE_BUSY_TIMEOUT_MS:
        raise LedgerValidationError("busy_timeout_ms must be an integer >= 0.")
    return resolved


def prepare_db_target(db_path: Path | str, *, dir_mode: int) -> tuple[str, Path | None]:
    """Ensure parent directories exist with safe permissions and return connect target."""
    if isinstance(db_path, str) and db_path.strip() == MEMORY_DB_SENTINEL:
        return MEMORY_DB_SENTINEL, None
    raw_str = str(db_path).strip()
    if not raw_str:
        raise LedgerValidationError("db_path must be a non-empty filesystem path.")
    resolved = Path(raw_str)
    parent = resolved.parent
    parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    if os.name == "posix":
        parent.chmod(dir_mode)
    return str(resolved), resolved


def open_sqlite_connection(connect_target: str, *, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open a SQLite connection configured with WAL mode, foreign keys, and timeout."""
    timeout_seconds = busy_timeout_ms / MILLISECONDS_PER_SECOND
    conn = sqlite3.connect(connect_target, timeout=timeout_seconds, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA synchronous = {SQLITE_SYNCHRONOUS_MODE}")
    except sqlite3.Error as exc:
        conn.close()
        raise MigrationError(f"Failed to configure SQLite pragmas: {exc}") from exc
    return conn


def _is_in_transaction(conn: sqlite3.Connection) -> bool:
    """Return `True` when `conn` currently has an open SQLite transaction."""
    return conn.in_transaction


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Execute SQLite statements atomically inside a `BEGIN IMMEDIATE` transaction."""
    if _is_in_transaction(conn):
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        if _is_in_transaction(conn):
            conn.execute("ROLLBACK")
        raise


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> None:
    """Apply pending migrations deterministically and verify applied checksums."""
    try:
        conn.execute(SCHEMA_MIGRATIONS_BOOTSTRAP_SQL)
        existing_rows = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version ASC"
        ).fetchall()
        existing_by_version = {int(row["version"]): row for row in existing_rows}

        for step in migrations:
            applied = existing_by_version.get(step.version)
            if applied is not None:
                if str(applied["checksum"]) != step.checksum:
                    raise MigrationError(
                        f"Checksum mismatch on migration v{step.version} ({step.name})."
                    )
                continue
            _execute_migration_step(conn, step)
    except sqlite3.Error as exc:
        raise MigrationError(f"SQLite migration failed: {exc}") from exc


def _execute_migration_step(conn: sqlite3.Connection, step: Migration) -> None:
    """Run a single pending migration and seed built-in records atomically."""
    now_iso = format_utc_iso(datetime.now(UTC))
    with transaction(conn):
        for statement in _split_sql_statements(step.sql):
            conn.execute(statement)
        if step.version == SCHEMA_VERSION_INITIAL:
            _seed_builtin_facets(conn, now_iso)
        conn.execute(
            """
            INSERT INTO schema_migrations (version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (step.version, step.name, step.checksum, now_iso),
        )


def _split_sql_statements(sql_script: str) -> list[str]:
    """Split a deterministic DDL migration script into individual SQL statements."""
    return [chunk.strip() for chunk in sql_script.split(";") if chunk.strip()]


def _seed_builtin_facets(conn: sqlite3.Connection, now_iso: str) -> None:
    """Seed the canonical built-in `workplace` enum `Facet` schema."""
    conn.execute(
        """
        INSERT INTO facets (key, value_type, allowed_values_json, description, builtin, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (
            BUILTIN_WORKPLACE_FACET_KEY,
            FACET_TYPE_ENUM,
            json.dumps(list(BUILTIN_WORKPLACE_VALUES)),
            BUILTIN_WORKPLACE_DESCRIPTION,
            now_iso,
        ),
    )


def list_applied_migrations(conn: sqlite3.Connection) -> list[MigrationRecord]:
    """Return ordered list of applied `MigrationRecord` entries."""
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version ASC"
    ).fetchall()
    return [
        MigrationRecord(
            version=int(row["version"]),
            name=str(row["name"]),
            checksum=str(row["checksum"]),
            applied_at=parse_utc_iso(str(row["applied_at"])),
        )
        for row in rows
    ]


# endregion
