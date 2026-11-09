"""Dedicated `agenda_schema_migrations` DDL and runner coexisting with `worktrack.core`."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Final

from worktrack.agenda.models import AGENDA_SCHEMA_VERSION_INITIAL
from worktrack.core.exceptions import MigrationError
from worktrack.core.models import MigrationRecord, format_utc_iso, parse_utc_iso
from worktrack.core.schema import (
    Migration,
    _fetch_existing_migrations,
    apply_migrations,
    transaction,
)

# region Constants
AGENDA_SCHEMA_MIGRATIONS_BOOTSTRAP_SQL: Final = """
CREATE TABLE IF NOT EXISTS agenda_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

AGENDA_MIGRATION_V1_SQL: Final = """
CREATE TABLE IF NOT EXISTS agenda_items (
    id TEXT PRIMARY KEY,
    activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    horizon TEXT NOT NULL CHECK (horizon IN ('today', 'this_week', 'backlog')),
    priority INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planned', 'in_progress', 'done', 'deferred')),
    budget_seconds INTEGER CHECK (budget_seconds IS NULL OR budget_seconds >= 0),
    deadline TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agenda_items_horizon_priority
    ON agenda_items(horizon, status, priority, created_at);

CREATE INDEX IF NOT EXISTS idx_agenda_items_activity
    ON agenda_items(activity_id);

CREATE TABLE IF NOT EXISTS focus_blocks (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_us INTEGER NOT NULL CHECK (duration_us > 0),
    activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
    label TEXT NOT NULL,
    expose_busy INTEGER NOT NULL DEFAULT 0 CHECK (expose_busy IN (0, 1)),
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_focus_blocks_window
    ON focus_blocks(started_at, ended_at);
"""
# endregion


# region Schema
AGENDA_MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        version=AGENDA_SCHEMA_VERSION_INITIAL,
        name="0001_initial_agenda_and_focus_blocks",
        sql=AGENDA_MIGRATION_V1_SQL,
    ),
)


def apply_agenda_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[Migration, ...] = AGENDA_MIGRATIONS,
) -> None:
    """Ensure core ledger tables exist, then apply `agenda_schema_migrations` steps."""
    apply_migrations(conn)
    try:
        existing_by_version = _fetch_existing_migrations(
            conn,
            "agenda_schema_migrations",
            AGENDA_SCHEMA_MIGRATIONS_BOOTSTRAP_SQL,
        )

        for step in migrations:
            applied = existing_by_version.get(step.version)
            if applied is not None:
                if str(applied["checksum"]) != step.checksum:
                    raise MigrationError(
                        f"Checksum mismatch on agenda migration v{step.version} ({step.name})."
                    )
                continue
            _execute_agenda_migration_step(conn, step)
    except sqlite3.Error as exc:
        raise MigrationError(f"Agenda SQLite migration failed: {exc}") from exc


def _execute_agenda_migration_step(conn: sqlite3.Connection, step: Migration) -> None:
    """Execute a single pending `agenda_schema_migrations` step atomically."""
    now_iso = format_utc_iso(datetime.now(UTC))
    statements = [chunk.strip() for chunk in step.sql.split(";") if chunk.strip()]
    with transaction(conn):
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            """
            INSERT INTO agenda_schema_migrations (version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (step.version, step.name, step.checksum, now_iso),
        )


def list_applied_agenda_migrations(conn: sqlite3.Connection) -> list[MigrationRecord]:
    """Return ordered list of applied `agenda_schema_migrations` audit records."""
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at "
        "FROM agenda_schema_migrations ORDER BY version ASC"
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
