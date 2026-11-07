"""`EntityCatalogSyncClient` pulling shared `Entities` and `Attribution` templates from `Hub`."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from worktrack.core import Entity, Ledger
from worktrack.core.models import format_utc_iso
from worktrack.core.schema import transaction
from worktrack.hub_sync.exceptions import HubTransportError
from worktrack.hub_sync.hub_sink import (
    HTTP_STATUS_OK_MAX,
    HTTP_STATUS_OK_MIN,
    HubSyncTransport,
    UrllibHubSyncTransport,
)
from worktrack.hub_sync.models import (
    DEFAULT_CATALOG_VERSION,
    HUB_CATALOG_SOURCE_SLUG,
    NODE_ID_HEADER_NAME,
    TRACE_ID_HEADER_NAME,
    CatalogAttributionTemplate,
    CatalogEntityRecord,
    EntityCatalogSnapshot,
    HubEndpointConfig,
)
from worktrack.secrets import UserTokenStore
from worktrack.telemetry.logging import resolve_trace_id
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
METRIC_SOURCE_SYNCS_TOTAL: Final = "worktrack_source_syncs_total"
SYNC_OUTCOME_OK: Final = "ok"
SYNC_OUTCOME_ERROR: Final = "error"
# endregion


# region Catalog Sync
@dataclass(frozen=True, slots=True)
class CatalogSyncResult:
    """Summary counts from pulling shared `Entities` and `Attributions` into a `Node` `Ledger`."""

    entities_created: int
    entities_updated: int
    entities_unchanged: int
    attributions_upserted: int
    attributions_skipped: int
    catalog_version: int = DEFAULT_CATALOG_VERSION

    def to_dict(self) -> dict[str, object]:
        """Serialize sync summary counts to a JSON-compatible dictionary."""
        return dataclasses.asdict(self)


class EntityCatalogSyncClient:
    """Pull shared `Entity` nodes and default `Attribution` templates from `Hub` into `Ledger`."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        transport: HubSyncTransport | None = None,
        config: HubEndpointConfig | None = None,
        token_store: UserTokenStore | None = None,
        telemetry: TelemetryRecorder | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize catalog sync client bound to local `Node` `Ledger` and `HubSyncTransport`."""
        self._ledger = ledger
        self._config = HubEndpointConfig(base_url="https://127.0.0.1") if config is None else config
        self._token_store = token_store
        self._telemetry = telemetry
        self._environ = environ
        self._transport: HubSyncTransport = (
            UrllibHubSyncTransport(
                config=self._config,
                token_store=token_store,
                environ=environ,
            )
            if transport is None
            else transport
        )

    def sync_catalog(self, *, trace_id: str | None = None) -> CatalogSyncResult:
        """Pull `EntityCatalogSnapshot` from `Hub` and apply it idempotently to `Ledger`."""
        try:
            snapshot = self.fetch_catalog_snapshot(trace_id=trace_id)
            result = self.apply_catalog_snapshot(snapshot)
        except Exception:
            self._record_sync_metric(SYNC_OUTCOME_ERROR)
            raise
        self._record_sync_metric(SYNC_OUTCOME_OK)
        return result

    def pull_catalog(self, *, trace_id: str | None = None) -> CatalogSyncResult:
        """Alias for `sync_catalog` pulling shared `Entities` and `Attribution` templates."""
        return self.sync_catalog(trace_id=trace_id)

    def fetch_catalog_snapshot(self, *, trace_id: str | None = None) -> EntityCatalogSnapshot:
        """Fetch and decode `EntityCatalogSnapshot` from `HubSyncTransport` without local egress."""
        headers = self._build_pull_headers(trace_id=trace_id)
        response = self._transport.pull_entity_catalog(headers=headers)
        if not (HTTP_STATUS_OK_MIN <= response.status_code <= HTTP_STATUS_OK_MAX):
            raise HubTransportError(
                f"Hub entity catalog pull failed with HTTP {response.status_code}.",
                http_status=response.status_code,
            )
        return EntityCatalogSnapshot.from_mapping(response.payload)

    def apply_catalog_snapshot(self, snapshot: EntityCatalogSnapshot) -> CatalogSyncResult:
        """Idempotently upsert catalog `Entities` and `Attribution` templates in `Ledger`."""
        created = 0
        updated = 0
        unchanged = 0
        for record in snapshot.entities:
            outcome = _upsert_local_catalog_entity(self._ledger, record)
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            else:
                unchanged += 1

        upserted = 0
        skipped = 0
        for template in snapshot.attribution_templates:
            if _apply_attribution_template(self._ledger, template):
                upserted += 1
            else:
                skipped += 1

        return CatalogSyncResult(
            entities_created=created,
            entities_updated=updated,
            entities_unchanged=unchanged,
            attributions_upserted=upserted,
            attributions_skipped=skipped,
            catalog_version=snapshot.catalog_version,
        )

    def _build_pull_headers(self, *, trace_id: str | None = None) -> dict[str, str]:
        """Build trace, node ID, and optional bearer headers for read-only catalog pull."""
        env = (
            self._environ
            if self._environ is not None
            else getattr(self._transport, "_environ", None)
        )
        headers: dict[str, str] = {
            TRACE_ID_HEADER_NAME: resolve_trace_id(
                environ=env,
                explicit_trace_id=trace_id,
            ),
            NODE_ID_HEADER_NAME: self._config.resolve_node_id(environ=env),
        }
        if self._token_store is not None:
            bundle = self._token_store.load(self._config.provider_name, self._config.account)
            if bundle is not None:
                headers["Authorization"] = f"{bundle.token_type} {bundle.access_token.expose()}"
        return headers

    def _record_sync_metric(self, outcome: str) -> None:
        """Increment `worktrack_source_syncs_total{source='hub',outcome}` when configured."""
        if self._telemetry is not None:
            self._telemetry.increment_counter(
                METRIC_SOURCE_SYNCS_TOTAL,
                {"source": HUB_CATALOG_SOURCE_SLUG, "outcome": outcome},
            )


# endregion


# region Entities
def _upsert_local_catalog_entity(
    ledger: Ledger,
    record: CatalogEntityRecord,
) -> str:
    """Upsert a catalog `Entity` in `ledger`, returning `'created'|'updated'|'unchanged'`."""
    merged_meta = {"catalog_source": HUB_CATALOG_SOURCE_SLUG, **dict(record.metadata)}
    existing = ledger.find_entity(record.kind, record.key)
    if existing is None:
        ledger.create_entity(
            record.kind,
            record.key,
            record.name,
            metadata=merged_meta,
            entity_id=record.entity_id,
        )
        return "created"
    if existing.name == record.name and existing.metadata == merged_meta:
        return "unchanged"
    _update_entity_in_place(ledger, existing, name=record.name, metadata=merged_meta)
    return "updated"


def _update_entity_in_place(
    ledger: Ledger,
    existing: Entity,
    *,
    name: str,
    metadata: Mapping[str, object],
) -> None:
    """Update `name` and `metadata_json` on an existing `Entity` preserving its primary ID."""
    conn: sqlite3.Connection = ledger._require_conn()
    now_iso = format_utc_iso(datetime.now(UTC))
    encoded_meta = json.dumps(dict(metadata), sort_keys=True)
    with transaction(conn):
        conn.execute(
            """
            UPDATE entities
            SET name = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, encoded_meta, now_iso, existing.id),
        )


# endregion


# region Attribution
def _apply_attribution_template(
    ledger: Ledger,
    template: CatalogAttributionTemplate,
) -> bool:
    """Upsert one `CatalogAttributionTemplate` into `ledger`, returning `True` if applied."""
    target = ledger.find_entity(template.target_kind, template.target_key)
    if target is None:
        return False

    if template.source_kind is not None and template.source_key is not None:
        source_ent = ledger.find_entity(template.source_kind, template.source_key)
        if source_ent is None:
            return False
        ledger.set_attribution(
            source_entity_id=source_ent.id,
            target_entity_id=target.id,
            weight=template.weight,
        )
        return True

    if template.source_activity_ref is not None:
        activity = ledger.find_activity_by_external_ref(template.source_activity_ref)
        if activity is None:
            return False
        ledger.set_attribution(
            source_activity_id=activity.id,
            target_entity_id=target.id,
            weight=template.weight,
        )
        return True

    return False


# endregion
