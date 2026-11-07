"""Dedicated `clue_schema_migrations` DDL and runner coexisting in `settings.db_path`."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Final

from worktrack.collectors.models import (
    CLUE_SCHEMA_VERSION_INITIAL,
    ClueMigrationError,
)
from worktrack.core.models import MigrationRecord, format_utc_iso, parse_utc_iso
from worktrack.core.schema import Migration, transaction

# region Constants
CLUE_SCHEMA_MIGRATIONS_BOOTSTRAP_SQL: Final = """
CREATE TABLE IF NOT EXISTS clue_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

CLUE_MIGRATION_V1_SQL: Final = """
CREATE TABLE IF NOT EXISTS clues (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    inferred_activity_key TEXT,
    activity_id TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (source, external_key)
);

CREATE INDEX IF NOT EXISTS idx_clues_occurred_at
    ON clues(occurred_at ASC, id ASC);

CREATE INDEX IF NOT EXISTS idx_clues_source_occurred
    ON clues(source, occurred_at ASC);

CREATE INDEX IF NOT EXISTS idx_clues_activity_key
    ON clues(inferred_activity_key, occurred_at ASC)
    WHERE inferred_activity_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_clues_activity_id
    ON clues(activity_id, occurred_at ASC)
    WHERE activity_id IS NOT NULL;
"""

CLUE_MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        version=CLUE_SCHEMA_VERSION_INITIAL,
        name="0001_initial_clues_table",
        sql=CLUE_MIGRATION_V1_SQL,
    ),
)
# endregion


# region Schema
def apply_clue_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[Migration, ...] = CLUE_MIGRATIONS,
) -> None:
    """Apply pending `clue_schema_migrations` steps and verify applied checksums."""
    try:
        conn.execute(CLUE_SCHEMA_MIGRATIONS_BOOTSTRAP_SQL)
        rows = conn.execute(
            "SELECT version, name, checksum FROM clue_schema_migrations ORDER BY version ASC"
        ).fetchall()
        existing = {int(row["version"]): row for row in rows}
        for step in migrations:
            applied = existing.get(step.version)
            if applied is not None:
                if str(applied["checksum"]) != step.checksum:
                    raise ClueMigrationError(
                        f"Checksum mismatch on clue migration v{step.version} ({step.name})."
                    )
                continue
            _execute_clue_migration_step(conn, step)
    except sqlite3.Error as exc:
        raise ClueMigrationError(f"Clue SQLite migration failed: {exc}") from exc


def _execute_clue_migration_step(conn: sqlite3.Connection, step: Migration) -> None:
    """Execute a single pending `clue_schema_migrations` step inside a transaction."""
    now_iso = format_utc_iso(datetime.now(UTC))
    statements = [chunk.strip() for chunk in step.sql.split(";") if chunk.strip()]
    with transaction(conn):
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            """
            INSERT INTO clue_schema_migrations (version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (step.version, step.name, step.checksum, now_iso),
        )


def list_applied_clue_migrations(conn: sqlite3.Connection) -> list[MigrationRecord]:
    """Return ordered list of applied `clue_schema_migrations` audit records."""
    rows = conn.execute(
        """
        SELECT version, name, checksum, applied_at
        FROM clue_schema_migrations
        ORDER BY version ASC
        """
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
