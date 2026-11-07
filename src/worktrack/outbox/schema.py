"""SQLite DDL and isolated `outbox_schema_migrations` lifecycle for `worktrack.outbox`."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Final

from worktrack.core.models import MigrationRecord, format_utc_iso, parse_utc_iso
from worktrack.core.schema import Migration, transaction
from worktrack.outbox.exceptions import OutboxMigrationError

# region Constants
OUTBOX_SCHEMA_VERSION_INITIAL: Final = 1

OUTBOX_MIGRATIONS_BOOTSTRAP_SQL: Final = """
CREATE TABLE IF NOT EXISTS outbox_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

OUTBOX_MIGRATION_V1_SQL: Final = """
CREATE TABLE IF NOT EXISTS outbox_items (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    sink TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    raw_duration_seconds REAL NOT NULL CHECK (raw_duration_seconds > 0.0),
    rounded_duration_seconds INTEGER NOT NULL CHECK (rounded_duration_seconds > 0),
    summary TEXT NOT NULL,
    cadence TEXT NOT NULL CHECK (
        cadence IN ('on_clock_out', 'eod', 'eow', 'eom', 'manual')
    ),
    status TEXT NOT NULL CHECK (
        status IN ('staged', 'approved', 'rejected', 'flushed', 'failed')
    ),
    source_interval_ids_json TEXT NOT NULL,
    payload_preview_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    rejection_reason TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_items_active_idempotency
    ON outbox_items(idempotency_key)
    WHERE status IN ('staged', 'approved');

CREATE INDEX IF NOT EXISTS idx_outbox_items_status_cadence
    ON outbox_items(status, cadence, sink);

CREATE INDEX IF NOT EXISTS idx_outbox_items_bucket_flushed
    ON outbox_items(sink, target_ref, window_start, window_end, updated_at)
    WHERE status = 'flushed';

CREATE TABLE IF NOT EXISTS egress_ledger (
    id TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    outbox_item_id TEXT NOT NULL REFERENCES outbox_items(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    sink TEXT NOT NULL,
    destination_uri TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'skipped_duplicate', 'error')),
    http_status INTEGER,
    external_receipt_id TEXT,
    trace_id TEXT NOT NULL,
    error_detail TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_egress_ledger_ok_idempotency
    ON egress_ledger(sink, idempotency_key)
    WHERE outcome = 'ok';

CREATE INDEX IF NOT EXISTS idx_egress_ledger_recorded_at
    ON egress_ledger(recorded_at, id);

CREATE TRIGGER IF NOT EXISTS trg_egress_ledger_no_update
BEFORE UPDATE ON egress_ledger
BEGIN
    SELECT RAISE(ABORT, 'egress_ledger is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_egress_ledger_no_delete
BEFORE DELETE ON egress_ledger
BEGIN
    SELECT RAISE(ABORT, 'egress_ledger is append-only: DELETE is forbidden');
END;
"""

OUTBOX_MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        version=OUTBOX_SCHEMA_VERSION_INITIAL,
        name="0001_initial_outbox_and_egress_ledger",
        sql=OUTBOX_MIGRATION_V1_SQL,
    ),
)
# endregion


# region Lifecycle
def apply_outbox_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[Migration, ...] = OUTBOX_MIGRATIONS,
) -> None:
    """Apply pending `outbox_schema_migrations` deterministically and verify checksums."""
    try:
        conn.execute(OUTBOX_MIGRATIONS_BOOTSTRAP_SQL)
        rows = conn.execute(
            "SELECT version, name, checksum FROM outbox_schema_migrations ORDER BY version ASC"
        ).fetchall()
        existing = {int(row["version"]): row for row in rows}
        for step in migrations:
            applied = existing.get(step.version)
            if applied is not None:
                if str(applied["checksum"]) != step.checksum:
                    raise OutboxMigrationError(
                        f"Checksum mismatch on outbox migration v{step.version} ({step.name})."
                    )
                continue
            _apply_single_outbox_migration(conn, step)
    except sqlite3.Error as exc:
        raise OutboxMigrationError(f"Outbox SQLite migration failed: {exc}") from exc


def _apply_single_outbox_migration(conn: sqlite3.Connection, step: Migration) -> None:
    """Execute a single pending outbox DDL script inside an atomic transaction."""
    now_iso = format_utc_iso(datetime.now(UTC))
    with transaction(conn):
        for stmt in _split_ddl_statements(step.sql):
            conn.execute(stmt)
        conn.execute(
            """
            INSERT INTO outbox_schema_migrations (version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (step.version, step.name, step.checksum, now_iso),
        )


def _split_ddl_statements(sql_script: str) -> list[str]:
    """Split SQL DDL including `BEGIN ... END;` trigger blocks into executable statements."""
    statements: list[str] = []
    buffer: list[str] = []
    inside_trigger = False
    for line in sql_script.splitlines():
        stripped_upper = line.strip().upper()
        if stripped_upper.startswith("CREATE TRIGGER"):
            inside_trigger = True
        buffer.append(line)
        if inside_trigger:
            if stripped_upper == "END;":
                statements.append("\n".join(buffer).strip())
                buffer.clear()
                inside_trigger = False
        elif stripped_upper.endswith(";"):
            candidate = "\n".join(buffer).strip()
            if candidate.rstrip(";").strip():
                statements.append(candidate)
            buffer.clear()
    return statements


def list_applied_outbox_migrations(conn: sqlite3.Connection) -> list[MigrationRecord]:
    """Return ordered list of applied `outbox_schema_migrations` records."""
    rows = conn.execute(
        """
        SELECT version, name, checksum, applied_at
        FROM outbox_schema_migrations
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
