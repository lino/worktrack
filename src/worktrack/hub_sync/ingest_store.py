"""Receiving `HubRollupIngestStore` for idempotent rollup ingest and shared `Entity` catalog."""

from __future__ import annotations

import dataclasses
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from worktrack.config import WorktrackSettings
from worktrack.core.models import (
    MigrationRecord,
    format_utc_iso,
    parse_utc_iso,
    validate_lower_slug,
    validate_non_empty_text,
)
from worktrack.core.schema import (
    DEFAULT_DB_DIR_MODE,
    open_sqlite_connection,
    prepare_db_target,
    resolve_busy_timeout_ms,
    transaction,
)
from worktrack.hub_sync.exceptions import HubRollupValidationError, HubSyncError
from worktrack.hub_sync.models import (
    DEFAULT_CATALOG_VERSION,
    DEFAULT_NODE_ID,
    MIN_POSITIVE_DURATION_SECONDS,
    CatalogAttributionTemplate,
    CatalogEntityRecord,
    EntityCatalogSnapshot,
    GangliaRollupEnvelope,
    IngestedHubRollup,
    validate_ganglia_rollup_payload,
    validate_node_id,
    validate_sha256_digest,
)
from worktrack.hub_sync.schema import (
    apply_hub_sync_migrations,
    assert_no_catalog_cycle,
    insert_ingested_rollup,
    list_applied_hub_sync_migrations,
    persist_attribution_template,
    persist_catalog_entity,
    row_to_attribution_template,
    row_to_catalog_entity,
    row_to_ingested_rollup,
)
from worktrack.outbox.models import DEFAULT_FLUSH_CADENCE
from worktrack.outbox.policies import compute_payload_sha256

# region Constants
EMPTY_SCHEMA_VERSION: Final = 0
# endregion


# region Validation
def parse_ingest_fields(
    raw: Mapping[str, object],
    *,
    node_id_override: str | None = None,
    expected_sha256: str | None = None,
) -> GangliaRollupEnvelope:
    """Validate and normalize an inbound rollup mapping into a `GangliaRollupEnvelope`."""
    validate_ganglia_rollup_payload(raw)
    node_candidate = node_id_override if node_id_override is not None else raw.get("node_id")
    resolved_node = validate_node_id(str(node_candidate or DEFAULT_NODE_ID))
    idem_key = validate_non_empty_text(
        str(raw.get("idempotency_key", "")), field_name="idempotency_key"
    )
    target_ref = validate_non_empty_text(str(raw.get("target_ref", "")), field_name="target_ref")
    preview, shares = _extract_preview_and_shares(raw)
    validate_ganglia_rollup_payload(preview)
    verified_sha = _verify_inbound_sha256(raw, preview, expected_sha256)
    duration = _extract_verified_duration(raw, preview)
    start_raw = raw.get("window_start", raw.get("started_at"))
    return GangliaRollupEnvelope(
        node_id=resolved_node,
        outbox_item_id=str(raw.get("outbox_item_id", idem_key)).strip(),
        idempotency_key=idem_key,
        target_ref=target_ref,
        window_start=parse_utc_iso(str(start_raw)),
        window_end=parse_utc_iso(str(raw.get("window_end"))),
        rounded_duration_seconds=duration,
        summary=str(raw.get("summary", target_ref)).strip(),
        cadence=str(raw.get("cadence", DEFAULT_FLUSH_CADENCE)).strip(),
        payload_preview=preview,
        payload_sha256=verified_sha,
        entity_shares=shares,
    )


def _extract_preview_and_shares(
    raw: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, float]]:
    """Extract normalized `payload_preview` and `entity_shares` dictionaries."""
    raw_preview = raw.get("payload_preview")
    if isinstance(raw_preview, Mapping):
        preview = dict(raw_preview)
    else:
        preview = {k: v for k, v in raw.items() if k != "payload_sha256"}
    raw_shares = raw.get("entity_shares")
    shares: dict[str, float] = {}
    if isinstance(raw_shares, Mapping):
        for key, val in raw_shares.items():
            if isinstance(val, (int, float)):
                shares[str(key)] = float(val)
    return preview, shares


def _optional_sha256(value: object, *, field_name: str = "payload_sha256") -> str | None:
    """Validate an optional SHA-256 digest string or return `None`."""
    if not isinstance(value, str) or not value.strip():
        return None
    return validate_sha256_digest(value, field_name=field_name)


def _verify_inbound_sha256(
    raw: Mapping[str, object],
    preview: Mapping[str, object],
    expected_sha256: str | None,
) -> str:
    """Verify inbound payload SHA-256 against `expected_sha256` and embedded `payload_sha256`."""
    stripped = dict(raw)
    stripped.pop("payload_sha256", None)
    preview_sha = compute_payload_sha256(preview)
    valid_candidates = {preview_sha, compute_payload_sha256(stripped), compute_payload_sha256(raw)}
    embedded_sha = _optional_sha256(raw.get("payload_sha256"))
    explicit_sha = _optional_sha256(expected_sha256, field_name="expected_sha256")
    provided = [sha for sha in (explicit_sha, embedded_sha) if sha is not None]
    if any(sha not in valid_candidates for sha in provided) or len(set(provided)) > 1:
        raise HubRollupValidationError("Rollup payload SHA-256 verification failed.")
    return provided[0] if provided else preview_sha


def _extract_verified_duration(raw: Mapping[str, object], preview: Mapping[str, object]) -> int:
    """Extract positive integer duration and ensure envelope/preview consistency."""
    raw_dur = raw.get("rounded_duration_seconds", raw.get("time_spent_seconds"))
    if (
        isinstance(raw_dur, bool)
        or not isinstance(raw_dur, int)
        or raw_dur < MIN_POSITIVE_DURATION_SECONDS
    ):
        raise HubRollupValidationError("rounded_duration_seconds must be an integer >= 1.")
    preview_dur = preview.get("time_spent_seconds", preview.get("rounded_duration_seconds"))
    if preview_dur is not None and preview_dur != raw_dur:
        raise HubRollupValidationError(
            "Envelope rounded_duration_seconds does not match payload_preview duration."
        )
    preview_target = preview.get("target_ref")
    if (
        isinstance(preview_target, str)
        and preview_target.strip() != str(raw.get("target_ref", "")).strip()
    ):
        raise HubRollupValidationError(
            "Envelope target_ref does not match payload_preview target_ref."
        )
    return raw_dur


# endregion


# region Hub Ingest Store
class HubRollupIngestStore:
    """Receiving `Hub` store persisting node aggregate rollups and shared `Entity` catalog."""

    def __init__(
        self,
        db_path: Path | str = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
        busy_timeout_ms: int | None = None,
        dir_mode: int = DEFAULT_DB_DIR_MODE,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Open or attach SQLite connection and apply `hub_sync_schema_migrations`."""
        resolved_timeout = resolve_busy_timeout_ms(busy_timeout_ms, environ=environ)
        if connection is None:
            connect_target, resolved_path = prepare_db_target(db_path, dir_mode=dir_mode)
            conn = open_sqlite_connection(connect_target, busy_timeout_ms=resolved_timeout)
            owns_conn = True
        else:
            conn, resolved_path, owns_conn = connection, None, False

        try:
            apply_hub_sync_migrations(conn)
        except Exception:
            if owns_conn:
                conn.close()
            raise

        self._conn = conn
        self._owns_conn = owns_conn
        self._db_path = resolved_path
        self._closed = False

    @classmethod
    def from_settings(
        cls,
        settings: WorktrackSettings,
        *,
        busy_timeout_ms: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Construct a `HubRollupIngestStore` bound to `settings.db_path`."""
        return cls(settings.db_path, busy_timeout_ms=busy_timeout_ms, environ=environ)

    @property
    def hub_sync_schema_version(self) -> int:
        """Return highest applied `hub_sync_schema_migrations` version."""
        row = (
            self._require_conn()
            .execute("SELECT COALESCE(MAX(version), 0) FROM hub_sync_schema_migrations")
            .fetchone()
        )
        return EMPTY_SCHEMA_VERSION if row is None else int(row[0])

    def applied_migrations(self) -> list[MigrationRecord]:
        """List applied `hub_sync_schema_migrations` records in ascending order."""
        return list_applied_hub_sync_migrations(self._require_conn())

    def close(self) -> None:
        """Close owned SQLite connection idempotently."""
        if not self._closed:
            if self._owns_conn:
                self._conn.close()
            self._closed = True

    def __enter__(self) -> Self:
        """Enter context manager returning this `HubRollupIngestStore`."""
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

    def ingest_rollup(
        self,
        payload: Mapping[str, object] | GangliaRollupEnvelope,
        *,
        node_id: str | None = None,
        expected_sha256: str | None = None,
    ) -> IngestedHubRollup:
        """Ingest a node aggregate `Rollup` idempotently by `(node_id, idempotency_key)`."""
        raw_map = payload.to_dict() if isinstance(payload, GangliaRollupEnvelope) else payload
        envelope = parse_ingest_fields(
            raw_map, node_id_override=node_id, expected_sha256=expected_sha256
        )
        conn = self._require_conn()
        existing = self.find_ingested_rollup(envelope.node_id, envelope.idempotency_key)
        if existing is not None:
            if existing.payload_sha256 != envelope.payload_sha256:
                msg = f"Conflicting SHA-256 for ({envelope.node_id}, {envelope.idempotency_key})."
                raise HubRollupValidationError(msg)
            return dataclasses.replace(existing, deduplicated=True)

        rollup_id = str(uuid.uuid4())
        now_iso = format_utc_iso(datetime.now(UTC))
        with transaction(conn):
            insert_ingested_rollup(conn, rollup_id, envelope, now_iso)
        return self.get_ingested_rollup(rollup_id)

    def get_ingested_rollup(self, rollup_id: str) -> IngestedHubRollup:
        """Fetch an `IngestedHubRollup` by primary identifier."""
        row = (
            self._require_conn()
            .execute("SELECT * FROM hub_ingested_rollups WHERE id = ?", (rollup_id.strip(),))
            .fetchone()
        )
        if row is None:
            raise HubSyncError(f"IngestedHubRollup '{rollup_id}' does not exist.")
        return row_to_ingested_rollup(row)

    def find_ingested_rollup(self, node_id: str, idempotency_key: str) -> IngestedHubRollup | None:
        """Find a previously ingested rollup by `(node_id, idempotency_key)`."""
        sql = "SELECT * FROM hub_ingested_rollups WHERE node_id = ? AND idempotency_key = ? LIMIT 1"
        row = (
            self._require_conn()
            .execute(sql, (validate_node_id(node_id), idempotency_key.strip()))
            .fetchone()
        )
        return None if row is None else row_to_ingested_rollup(row)

    def list_ingested_rollups(
        self,
        *,
        node_id: str | None = None,
        target_ref: str | None = None,
    ) -> list[IngestedHubRollup]:
        """List ingested node rollups filtered by optional `node_id` and `target_ref`."""
        clauses: list[str] = []
        params: list[object] = []
        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(validate_node_id(node_id))
        if target_ref is not None:
            clauses.append("target_ref = ?")
            params.append(target_ref.strip())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = (
            self._require_conn()
            .execute(
                f"SELECT * FROM hub_ingested_rollups {where_sql} ORDER BY window_start ASC, id ASC",
                params,
            )
            .fetchall()
        )
        return [row_to_ingested_rollup(row) for row in rows]

    def upsert_catalog_entity(
        self,
        kind: str,
        key: str,
        name: str,
        *,
        metadata: Mapping[str, object] | None = None,
        entity_id: str | None = None,
    ) -> CatalogEntityRecord:
        """Create or update a shared `Entity` node in the Hub catalog."""
        record = CatalogEntityRecord(
            kind=kind,
            key=key,
            name=name,
            metadata={} if metadata is None else dict(metadata),
            entity_id=entity_id or str(uuid.uuid4()),
        )
        now_iso = format_utc_iso(datetime.now(UTC))
        conn = self._require_conn()
        with transaction(conn):
            return persist_catalog_entity(conn, record, now_iso)

    def list_catalog_entities(self, *, kind: str | None = None) -> list[CatalogEntityRecord]:
        """List shared Hub catalog `Entity` records optionally filtered by `kind`."""
        conn = self._require_conn()
        if kind is not None:
            clean_kind = validate_lower_slug(kind, field_name="Entity kind")
            rows = conn.execute(
                "SELECT * FROM hub_catalog_entities WHERE kind = ? ORDER BY kind ASC, key ASC",
                (clean_kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM hub_catalog_entities ORDER BY kind ASC, key ASC"
            ).fetchall()
        return [row_to_catalog_entity(row) for row in rows]

    def upsert_attribution_template(
        self,
        *,
        target_kind: str,
        target_key: str,
        weight: float,
        source_kind: str | None = None,
        source_key: str | None = None,
        source_activity_ref: str | None = None,
    ) -> CatalogAttributionTemplate:
        """Create or update a default `Attribution` edge template in the Hub catalog."""
        template = CatalogAttributionTemplate(
            target_kind=target_kind,
            target_key=target_key,
            weight=weight,
            source_kind=source_kind,
            source_key=source_key,
            source_activity_ref=source_activity_ref,
        )
        conn = self._require_conn()
        with transaction(conn):
            if template.source_kind is not None and template.source_key is not None:
                assert_no_catalog_cycle(
                    conn,
                    source_pair=(template.source_kind, template.source_key),
                    target_pair=(template.target_kind, template.target_key),
                )
            persist_attribution_template(conn, template)
        return template

    def list_attribution_templates(self) -> list[CatalogAttributionTemplate]:
        """List all default `Attribution` templates in deterministic order."""
        rows = (
            self._require_conn()
            .execute(
                "SELECT * FROM hub_catalog_attribution_templates ORDER BY created_at ASC, id ASC"
            )
            .fetchall()
        )
        return [row_to_attribution_template(row) for row in rows]

    def get_catalog_snapshot(
        self, *, catalog_version: int = DEFAULT_CATALOG_VERSION
    ) -> EntityCatalogSnapshot:
        """Build an `EntityCatalogSnapshot` containing shared entities and templates."""
        return EntityCatalogSnapshot(
            entities=tuple(self.list_catalog_entities()),
            attribution_templates=tuple(self.list_attribution_templates()),
            catalog_version=catalog_version,
        )

    def _require_conn(self) -> sqlite3.Connection:
        """Return active SQLite connection or raise `HubSyncError` if closed."""
        if self._closed:
            raise HubSyncError("HubRollupIngestStore SQLite connection is closed.")
        return self._conn


# endregion
