"""SQLite DDL, `hub_sync_schema_migrations`, and persistence helpers for `worktrack.hub_sync`."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from worktrack.core.models import MigrationRecord, format_utc_iso, parse_utc_iso
from worktrack.core.schema import Migration, transaction
from worktrack.hub_sync.exceptions import HubMigrationError, HubRollupValidationError
from worktrack.outbox.policies import canonical_payload_json

if TYPE_CHECKING:
    from worktrack.hub_sync.models import (
        CatalogAttributionTemplate,
        CatalogEntityRecord,
        GangliaRollupEnvelope,
        IngestedHubRollup,
    )

# region Constants
HUB_SYNC_SCHEMA_VERSION_INITIAL: Final = 1

HUB_SYNC_MIGRATIONS_BOOTSTRAP_SQL: Final = """
CREATE TABLE IF NOT EXISTS hub_sync_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

HUB_SYNC_MIGRATION_V1_SQL: Final = """
CREATE TABLE IF NOT EXISTS hub_ingested_rollups (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    outbox_item_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    rounded_duration_seconds INTEGER NOT NULL CHECK (rounded_duration_seconds > 0),
    summary TEXT NOT NULL,
    cadence TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    entity_shares_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE (node_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_hub_ingested_rollups_node_window
    ON hub_ingested_rollups(node_id, window_start, target_ref);

CREATE TABLE IF NOT EXISTS hub_catalog_entities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, key)
);

CREATE INDEX IF NOT EXISTS idx_hub_catalog_entities_kind_key
    ON hub_catalog_entities(kind, key);

CREATE TABLE IF NOT EXISTS hub_catalog_attribution_templates (
    id TEXT PRIMARY KEY,
    source_kind TEXT,
    source_key TEXT,
    source_activity_ref TEXT,
    target_kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    weight REAL NOT NULL CHECK (weight > 0.0 AND weight <= 1.0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_hub_catalog_attr_entity_edge
    ON hub_catalog_attribution_templates(source_kind, source_key, target_kind, target_key)
    WHERE source_kind IS NOT NULL AND source_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_hub_catalog_attr_activity_edge
    ON hub_catalog_attribution_templates(source_activity_ref, target_kind, target_key)
    WHERE source_activity_ref IS NOT NULL;
"""

HUB_SYNC_MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        version=HUB_SYNC_SCHEMA_VERSION_INITIAL,
        name="0001_initial_hub_rollup_ingest_and_entity_catalog",
        sql=HUB_SYNC_MIGRATION_V1_SQL,
    ),
)
# endregion


# region Lifecycle
def apply_hub_sync_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[Migration, ...] = HUB_SYNC_MIGRATIONS,
) -> None:
    """Apply pending `hub_sync_schema_migrations` deterministically and verify checksums."""
    try:
        conn.execute(HUB_SYNC_MIGRATIONS_BOOTSTRAP_SQL)
        rows = conn.execute(
            "SELECT version, name, checksum FROM hub_sync_schema_migrations ORDER BY version ASC"
        ).fetchall()
        existing = {int(row["version"]): row for row in rows}
        for step in migrations:
            applied = existing.get(step.version)
            if applied is not None:
                if str(applied["checksum"]) != step.checksum:
                    raise HubMigrationError(
                        f"Checksum mismatch on hub_sync migration v{step.version} ({step.name})."
                    )
                continue
            _apply_single_hub_sync_migration(conn, step)
    except sqlite3.Error as exc:
        raise HubMigrationError(f"Hub sync SQLite migration failed: {exc}") from exc


def _apply_single_hub_sync_migration(conn: sqlite3.Connection, step: Migration) -> None:
    """Execute a single pending `hub_sync` DDL script inside an atomic transaction."""
    now_iso = format_utc_iso(datetime.now(UTC))
    with transaction(conn):
        for stmt in _split_ddl_statements(step.sql):
            conn.execute(stmt)
        conn.execute(
            """
            INSERT INTO hub_sync_schema_migrations (version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (step.version, step.name, step.checksum, now_iso),
        )


def _split_ddl_statements(sql_script: str) -> list[str]:
    """Split semicolon-delimited DDL statements into executable SQL strings."""
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql_script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            candidate = "\n".join(buffer).strip()
            if candidate.rstrip(";").strip():
                statements.append(candidate)
            buffer.clear()
    return statements


def list_applied_hub_sync_migrations(conn: sqlite3.Connection) -> list[MigrationRecord]:
    """Return ordered list of applied `hub_sync_schema_migrations` records."""
    sql = (
        "SELECT version, name, checksum, applied_at "
        "FROM hub_sync_schema_migrations ORDER BY version ASC"
    )
    rows = conn.execute(sql).fetchall()
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


# region Persistence
def insert_ingested_rollup(
    conn: sqlite3.Connection,
    rollup_id: str,
    envelope: GangliaRollupEnvelope,
    now_iso: str,
) -> None:
    """Insert a verified `GangliaRollupEnvelope` into `hub_ingested_rollups`."""
    conn.execute(
        """
        INSERT INTO hub_ingested_rollups (
            id, node_id, outbox_item_id, idempotency_key, target_ref,
            window_start, window_end, rounded_duration_seconds, summary,
            cadence, payload_json, payload_sha256, entity_shares_json, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rollup_id,
            envelope.node_id,
            envelope.outbox_item_id,
            envelope.idempotency_key,
            envelope.target_ref,
            format_utc_iso(envelope.window_start),
            format_utc_iso(envelope.window_end),
            envelope.rounded_duration_seconds,
            envelope.summary,
            envelope.cadence,
            canonical_payload_json(dict(envelope.payload_preview)),
            envelope.payload_sha256,
            canonical_payload_json(dict(envelope.entity_shares)),
            now_iso,
        ),
    )


def persist_catalog_entity(
    conn: sqlite3.Connection,
    record: CatalogEntityRecord,
    now_iso: str,
) -> CatalogEntityRecord:
    """Upsert a `CatalogEntityRecord` row into `hub_catalog_entities` and return hydrated record."""
    meta_json = canonical_payload_json(dict(record.metadata))
    conn.execute(
        """
        INSERT INTO hub_catalog_entities
            (id, kind, key, name, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, key)
        DO UPDATE SET
            name = excluded.name,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (record.entity_id, record.kind, record.key, record.name, meta_json, now_iso, now_iso),
    )
    row = conn.execute(
        "SELECT * FROM hub_catalog_entities WHERE kind = ? AND key = ?",
        (record.kind, record.key),
    ).fetchone()
    assert row is not None
    return row_to_catalog_entity(row)


def persist_attribution_template(
    conn: sqlite3.Connection,
    template: CatalogAttributionTemplate,
) -> None:
    """Upsert a `CatalogAttributionTemplate` row into `hub_catalog_attribution_templates`."""
    now_iso = format_utc_iso(datetime.now(UTC))
    row_id = str(uuid.uuid4())
    if template.source_kind is not None and template.source_key is not None:
        conn.execute(
            """
            INSERT INTO hub_catalog_attribution_templates (
                id, source_kind, source_key, source_activity_ref,
                target_kind, target_key, weight, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(source_kind, source_key, target_kind, target_key)
                WHERE source_kind IS NOT NULL AND source_key IS NOT NULL
            DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at
            """,
            (
                row_id,
                template.source_kind,
                template.source_key,
                template.target_kind,
                template.target_key,
                template.weight,
                now_iso,
                now_iso,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO hub_catalog_attribution_templates (
                id, source_kind, source_key, source_activity_ref,
                target_kind, target_key, weight, created_at, updated_at
            ) VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_activity_ref, target_kind, target_key)
                WHERE source_activity_ref IS NOT NULL
            DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at
            """,
            (
                row_id,
                template.source_activity_ref,
                template.target_kind,
                template.target_key,
                template.weight,
                now_iso,
                now_iso,
            ),
        )


def assert_no_catalog_cycle(
    conn: sqlite3.Connection,
    *,
    source_pair: tuple[str, str],
    target_pair: tuple[str, str],
) -> None:
    """Reject multi-hop cycles in catalog `Entity -> Entity` attribution templates via BFS."""
    rows = conn.execute(
        """
        SELECT source_kind, source_key, target_kind, target_key
        FROM hub_catalog_attribution_templates
        WHERE source_kind IS NOT NULL AND source_key IS NOT NULL
        """
    ).fetchall()
    adjacency: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row in rows:
        src = (str(row["source_kind"]), str(row["source_key"]))
        dst = (str(row["target_kind"]), str(row["target_key"]))
        adjacency.setdefault(src, []).append(dst)

    queue: deque[tuple[str, str]] = deque([target_pair])
    visited: set[tuple[str, str]] = {target_pair}
    while queue:
        current = queue.popleft()
        for neighbor in adjacency.get(current, ()):
            if neighbor == source_pair:
                raise HubRollupValidationError(
                    f"Catalog attribution edge {source_pair} -> {target_pair} creates a cycle."
                )
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)


def row_to_ingested_rollup(row: sqlite3.Row) -> IngestedHubRollup:
    """Hydrate an `IngestedHubRollup` from a `hub_ingested_rollups` SQLite row."""
    from worktrack.hub_sync.models import IngestedHubRollup

    raw_shares = json.loads(str(row["entity_shares_json"]))
    shares = (
        {str(k): float(v) for k, v in raw_shares.items()} if isinstance(raw_shares, Mapping) else {}
    )
    return IngestedHubRollup(
        id=str(row["id"]),
        node_id=str(row["node_id"]),
        outbox_item_id=str(row["outbox_item_id"]),
        idempotency_key=str(row["idempotency_key"]),
        target_ref=str(row["target_ref"]),
        window_start=parse_utc_iso(str(row["window_start"])),
        window_end=parse_utc_iso(str(row["window_end"])),
        rounded_duration_seconds=int(row["rounded_duration_seconds"]),
        summary=str(row["summary"]),
        cadence=str(row["cadence"]),
        payload_sha256=str(row["payload_sha256"]),
        entity_shares=shares,
        ingested_at=parse_utc_iso(str(row["ingested_at"])),
        deduplicated=False,
    )


def row_to_catalog_entity(row: sqlite3.Row) -> CatalogEntityRecord:
    """Hydrate a `CatalogEntityRecord` from a `hub_catalog_entities` SQLite row."""
    from worktrack.hub_sync.models import CatalogEntityRecord

    parsed_meta = json.loads(str(row["metadata_json"]))
    return CatalogEntityRecord(
        entity_id=str(row["id"]),
        kind=str(row["kind"]),
        key=str(row["key"]),
        name=str(row["name"]),
        metadata=dict(parsed_meta) if isinstance(parsed_meta, Mapping) else {},
    )


def row_to_attribution_template(row: sqlite3.Row) -> CatalogAttributionTemplate:
    """Hydrate a `CatalogAttributionTemplate` from a SQLite row."""
    from worktrack.hub_sync.models import CatalogAttributionTemplate

    raw_s_kind, raw_s_key, raw_act = (
        row["source_kind"],
        row["source_key"],
        row["source_activity_ref"],
    )
    return CatalogAttributionTemplate(
        source_kind=None if raw_s_kind is None else str(raw_s_kind),
        source_key=None if raw_s_key is None else str(raw_s_key),
        source_activity_ref=None if raw_act is None else str(raw_act),
        target_kind=str(row["target_kind"]),
        target_key=str(row["target_key"]),
        weight=float(row["weight"]),
    )


# endregion
