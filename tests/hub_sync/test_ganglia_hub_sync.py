"""Behavioural tests for `GangliaHubSink`, `HubRollupIngestStore`, and `EntityCatalogSyncClient`."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sqlite3
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worktrack import core, hub_sync, outbox
from worktrack.config import WorktrackSettings
from worktrack.core import Ledger
from worktrack.hub_sync import GangliaHubSink, HubEndpointConfig, HubRollupIngestStore
from worktrack.outbox import OutboxDispatcher, OutboxItem, OutboxStore
from worktrack.secrets import OAuthTokenBundle, SecretValue, UserTokenStore
from worktrack.telemetry import TelemetryRecorder

# region Constants
WORK_START = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)
WORK_DURATION_SECONDS = 2_700
ONE_HOUR_SECONDS = 3_600
FRESH_EXPIRY_SECONDS = 3_600
HTTP_OK = 200
HTTP_CREATED = 201
WEIGHT_FULL = 1.0
WEIGHT_SIXTY = 0.6
WEIGHT_FORTY = 0.4
EXPECTED_TRACE_ID = "7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d"
NODE_ALPHA_ID = "node-alpha-01"
SHA256_HEX_LEN = 64
HUB_URL = "https://hub.example.org"
# endregion


# region Fixtures
class _InMemoryKeyring:
    """In-memory `KeyringBackend` double for `UserTokenStore` in Hub sync tests."""

    def __init__(self) -> None:
        """Initialize empty in-memory credential dictionary."""
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        """Return stored secret string or `None`."""
        return self._store.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """Persist secret string for `(service_name, username)`."""
        self._store[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        """Remove secret string for `(service_name, username)`."""
        self._store.pop((service_name, username), None)


class _RecordingUrllibResponse:
    """Context-manager response double for `UrllibHubSyncTransport` urlopen tests."""

    def __init__(self, status: int, payload: Mapping[str, object]) -> None:
        """Initialize fake HTTP response with JSON payload and status code."""
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        """Return encoded UTF-8 JSON body bytes."""
        return self._raw

    def __enter__(self) -> _RecordingUrllibResponse:
        """Enter context manager."""
        return self

    def __exit__(self, *args: object) -> None:
        """Exit context manager cleanly."""


def _seed_token_store(tmp_path: Path, token_secret: str = "hub_bearer_secret_01") -> UserTokenStore:
    """Create a workstation `UserTokenStore` seeded with a `hub` OAuth bearer token."""
    fallback = tmp_path / "hub_tokens.json"
    store = UserTokenStore(keyring_backend=_InMemoryKeyring(), fallback_path=fallback)
    bundle = OAuthTokenBundle(
        access_token=SecretValue(token_secret), expires_in=FRESH_EXPIRY_SECONDS
    )
    store.store(hub_sync.HUB_PROVIDER_SLUG, bundle)
    return store


def _stage_hub_rollup_item(
    ledger: Ledger,
    store: OutboxStore,
    *,
    external_ref: str = "WT-401",
    telemetry: TelemetryRecorder | None = None,
) -> OutboxItem:
    """Record a closed interval with private local notes and stage a `hub` rollup item."""
    act = ledger.find_activity_by_external_ref(external_ref) or ledger.create_activity(
        f"Deliver {external_ref}", external_ref=external_ref
    )
    end_at = WORK_START + timedelta(seconds=WORK_DURATION_SECONDS)
    note = "Strictly private local scratchpad note never sent to Hub"
    ledger.record_structured_interval(act.id, started_at=WORK_START, ended_at=end_at, notes=note)
    policy = outbox.RollupPolicy(
        grouping_window=outbox.GROUPING_WINDOW_DAY,
        rounding_quantum_seconds=outbox.QUANTUM_FIFTEEN_MINUTES,
        redact_notes=True,
        flush_cadence=outbox.FLUSH_CADENCE_EOD,
    )
    engine = outbox.RollupEngine(ledger=ledger, outbox_store=store, telemetry=telemetry)
    return engine.stage_rollup(sink=hub_sync.HUB_SINK_SLUG, policy=policy)[-1]


def _seed_hub_entity_catalog(ingest_store: HubRollupIngestStore) -> None:
    """Seed shared `Entities` and default `Attribution` templates in `ingest_store`."""
    for kind, key, name in (
        (core.ENTITY_KIND_PROJECT, "worktrack-core", "Worktrack Core"),
        (core.ENTITY_KIND_COMPONENT, "hub-sync", "Ganglia Hub Sync"),
        (core.ENTITY_KIND_CUSTOMER, "cust-acme", "ACME Corp"),
        (core.ENTITY_KIND_CUSTOMER, "cust-globex", "Globex Corp"),
    ):
        ingest_store.upsert_catalog_entity(kind, key, name)
    for target_kind, target_key, weight in (
        (core.ENTITY_KIND_PROJECT, "worktrack-core", WEIGHT_FULL),
        (core.ENTITY_KIND_CUSTOMER, "cust-acme", WEIGHT_SIXTY),
        (core.ENTITY_KIND_CUSTOMER, "cust-globex", WEIGHT_FORTY),
    ):
        ingest_store.upsert_attribution_template(
            source_kind=core.ENTITY_KIND_COMPONENT,
            source_key="hub-sync",
            target_kind=target_kind,
            target_key=target_key,
            weight=weight,
        )
    ingest_store.upsert_attribution_template(
        source_activity_ref="WT-300",
        target_kind=core.ENTITY_KIND_COMPONENT,
        target_key="hub-sync",
        weight=WEIGHT_FULL,
    )


# endregion


# region Hub Sink & Egress Gate Tests
def test_ganglia_hub_sink_enforces_outbox_grant_and_blocks_bypasses(tmp_path: Path) -> None:
    """Verify `GangliaHubSink` blocks `None`, unapproved, and tampered grants with metrics."""
    node_db, hub_db = tmp_path / "node_bypass.sqlite3", tmp_path / "hub_bypass.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    cfg = HubEndpointConfig(base_url=HUB_URL, node_id=NODE_ALPHA_ID)

    with (
        Ledger(node_db) as ledger,
        OutboxStore(node_db, telemetry=telemetry) as store,
        HubRollupIngestStore(hub_db) as ingest_store,
    ):
        transport = hub_sync.LoopbackHubSyncTransport(ingest_store=ingest_store)
        sink = GangliaHubSink(transport=transport, config=cfg, telemetry=telemetry)
        assert sink.sink_name == hub_sync.HUB_SINK_SLUG
        staged = _stage_hub_rollup_item(ledger, store, telemetry=telemetry)
        with pytest.raises(outbox.EgressBypassBlockedError):
            sink.write_rollup(staged, grant=None, source="direct_call")
        with pytest.raises(outbox.EgressBypassBlockedError):
            hub_sync.verify_outbox_grant(
                item=staged, grant=None, sink_name="hub", telemetry=telemetry, source="unapproved"
            )
        approved = store.approve_item(staged.id)
        grant = store.issue_grant(approved.id)
        tampered_preview = {**approved.payload_preview, "time_spent_seconds": ONE_HOUR_SECONDS}
        tampered = dataclasses.replace(approved, payload_preview=tampered_preview)
        with pytest.raises(outbox.EgressBypassBlockedError):
            sink.write_rollup(tampered, grant=grant, source="payload_tampered")
        assert (transport.pushed_rollups, ingest_store.list_ingested_rollups()) == ([], [])

    exposition = telemetry.render_metrics_text()
    for src in ("direct_call", "unapproved", "payload_tampered"):
        metric = f'worktrack_egress_bypass_blocked_total{{sink="hub",source="{src}"}} 1.0'
        assert metric in exposition


@pytest.mark.parametrize(
    "forbidden_key",
    ("raw_intervals", "notes", "clues", "context_facts", "access_token"),
)
def test_ganglia_hub_sink_rejects_forbidden_raw_payload_keys(
    tmp_path: Path, forbidden_key: str
) -> None:
    """Verify `GangliaHubSink` rejects any payload containing forbidden raw or secret keys."""
    node_db = tmp_path / f"node_{forbidden_key}.sqlite3"
    hub_db = tmp_path / f"hub_{forbidden_key}.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / f"tel_{forbidden_key}")
    cfg = HubEndpointConfig(base_url=HUB_URL, node_id=NODE_ALPHA_ID)

    with pytest.raises(hub_sync.HubRollupValidationError):
        hub_sync.validate_ganglia_rollup_payload(
            {"sink": "hub", "time_spent_seconds": WORK_DURATION_SECONDS, forbidden_key: ["leak"]}
        )

    with (
        Ledger(node_db) as ledger,
        OutboxStore(node_db, telemetry=telemetry) as store,
        HubRollupIngestStore(hub_db) as ingest_store,
    ):
        transport = hub_sync.LoopbackHubSyncTransport(ingest_store=ingest_store)
        sink = GangliaHubSink(transport=transport, config=cfg, telemetry=telemetry)
        approved = store.approve_item(_stage_hub_rollup_item(ledger, store).id)
        grant = store.issue_grant(approved.id)
        injected = {**approved.payload_preview, forbidden_key: "forbidden-surveillance-data"}
        injected_sha = outbox.compute_payload_sha256(injected)
        poisoned = dataclasses.replace(
            approved, payload_preview=injected, payload_sha256=injected_sha
        )
        poisoned_grant = dataclasses.replace(grant, payload_sha256=injected_sha)
        with pytest.raises(hub_sync.HubEgressBypassError):
            sink.write_rollup(poisoned, grant=poisoned_grant, source="raw_key_probe")
        assert transport.pushed_rollups == []

    metric = 'worktrack_egress_bypass_blocked_total{sink="hub",source="raw_key_probe"} 1.0'
    assert metric in telemetry.render_metrics_text()


def test_ganglia_hub_sink_flushes_via_urllib_transport_with_trace_and_bearer_auth(
    tmp_path: Path,
) -> None:
    """Verify `UrllibHubSyncTransport` propagates `X-Fritz-Trace-Id` and bearer auth."""
    node_db, hub_db = tmp_path / "node_flush.sqlite3", tmp_path / "hub_flush.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    token_store = _seed_token_store(tmp_path, token_secret="hub_token_xyz987")
    captured: list[tuple[str, str, dict[str, str], dict[str, object]]] = []
    cfg = HubEndpointConfig(base_url=HUB_URL, node_id=NODE_ALPHA_ID)
    env = {"FRITZ_TRACE_ID": EXPECTED_TRACE_ID}

    with (
        Ledger(node_db) as ledger,
        OutboxStore(node_db, telemetry=telemetry) as store,
        HubRollupIngestStore(hub_db) as ingest,
    ):

        def _urlopen(
            req: urllib.request.Request, timeout: float = 15.0
        ) -> _RecordingUrllibResponse:
            """Route `urllib.request.Request` into `HubRollupIngestStore` and record headers."""
            raw_bytes = req.data.decode("utf-8") if isinstance(req.data, bytes) else "{}"
            body = json.loads(raw_bytes if timeout > 0 else "{}")
            captured.append((req.get_method(), req.full_url, dict(req.header_items()), body))
            return _RecordingUrllibResponse(HTTP_CREATED, ingest.ingest_rollup(body).to_dict())

        tr = hub_sync.UrllibHubSyncTransport(
            config=cfg, token_store=token_store, environ=env, urlopen=_urlopen
        )
        sink = GangliaHubSink(transport=tr, config=cfg, telemetry=telemetry, environ=env)
        staged = store.approve_item(
            _stage_hub_rollup_item(ledger, store, external_ref="WT-777", telemetry=telemetry).id
        )
        disp = OutboxDispatcher(outbox_store=store, sinks=[sink], telemetry=telemetry, environ=env)
        first = disp.flush_item(staged.id, trigger=outbox.FLUSH_TRIGGER_MANUAL)
        second = disp.flush_item(staged.id, trigger=outbox.FLUSH_TRIGGER_MANUAL)
        assert (first.outcome, second.outcome) == ("ok", "skipped_duplicate")
        assert (store.get_item(staged.id).status, len(captured)) == ("flushed", 1)
        receipts = store.list_egress_records(sink=hub_sync.HUB_SINK_SLUG)
        assert (len(receipts), len(receipts[0].payload_sha256)) == (2, SHA256_HEX_LEN)
        assert receipts[0].trace_id == EXPECTED_TRACE_ID

    method, url, hdrs, body = captured[0]
    lower_hdrs = {k.lower(): v for k, v in hdrs.items()}
    assert (method, url) == ("POST", f"{HUB_URL}{hub_sync.DEFAULT_ROLLUP_INGEST_PATH}")
    assert lower_hdrs["authorization"] == "Bearer hub_token_xyz987"
    assert lower_hdrs[hub_sync.TRACE_ID_HEADER_NAME.lower()] == EXPECTED_TRACE_ID
    assert lower_hdrs[hub_sync.IDEMPOTENCY_HEADER_NAME.lower()] == staged.idempotency_key
    assert lower_hdrs[hub_sync.NODE_ID_HEADER_NAME.lower()] == NODE_ALPHA_ID
    assert lower_hdrs[hub_sync.PAYLOAD_SHA256_HEADER_NAME.lower()] == staged.payload_sha256
    assert "Strictly private local scratchpad" not in json.dumps(body)
    exposition = telemetry.render_metrics_text()
    assert 'worktrack_outbox_flushes_total{cadence="eod",outcome="ok",sink="hub"} 1.0' in exposition
    assert 'outcome="skipped_duplicate",sink="hub"} 1.0' in exposition


# endregion


# region Hub Ingest Store Tests
def test_hub_rollup_ingest_store_migrations_idempotency_and_sha256_verification(
    tmp_path: Path,
) -> None:
    """Verify `HubRollupIngestStore` coexists in SQLite, deduplicates, and checks SHA-256."""
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    }
    settings = WorktrackSettings.from_env(environ=env, home_dir=tmp_path)
    with (
        Ledger.from_settings(settings) as ledger,
        OutboxStore.from_settings(settings) as outbox_store,
        HubRollupIngestStore.from_settings(settings) as ingest_store,
    ):
        assert ledger.schema_version == core.SCHEMA_VERSION_INITIAL
        assert ingest_store.hub_sync_schema_version == hub_sync.HUB_SYNC_SCHEMA_VERSION_INITIAL
        assert len(ingest_store.applied_migrations()) == 1

        staged = _stage_hub_rollup_item(ledger, outbox_store, external_ref="WT-900")
        envelope = hub_sync.GangliaRollupEnvelope.from_outbox_item(
            staged, node_id=NODE_ALPHA_ID, entity_shares={"project:wt": WEIGHT_FULL}
        ).to_dict()
        valid_sha = staged.payload_sha256
        first = ingest_store.ingest_rollup(envelope, expected_sha256=valid_sha)
        second = ingest_store.ingest_rollup(envelope, expected_sha256=valid_sha)
        assert (first.deduplicated, second.deduplicated, second.id) == (False, True, first.id)
        assert len(ingest_store.list_ingested_rollups(node_id=NODE_ALPHA_ID)) == 1

        with pytest.raises(hub_sync.HubRollupValidationError):
            tampered_dur = {**envelope, "rounded_duration_seconds": ONE_HOUR_SECONDS}
            ingest_store.ingest_rollup(tampered_dur, expected_sha256=valid_sha)
        with pytest.raises(hub_sync.HubRollupValidationError):
            new_key = {**envelope, "idempotency_key": "idem-hub-901"}
            ingest_store.ingest_rollup(new_key, expected_sha256="0" * SHA256_HEX_LEN)

    with contextlib.closing(sqlite3.connect(settings.db_path)) as raw_conn:
        rows = raw_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        tables = {str(row[0]) for row in rows}
    assert {"schema_migrations", "hub_sync_schema_migrations", "hub_ingested_rollups"} <= tables
    assert {"hub_catalog_entities", "hub_catalog_attribution_templates"} <= tables


# endregion


# region Entity Catalog Sync Tests
def test_entity_catalog_sync_client_pulls_shared_entities_and_attributions_idempotently(
    tmp_path: Path,
) -> None:
    """Verify `EntityCatalogSyncClient` syncs `Entities` and `Attributions` without local egress."""
    node_db, hub_db = tmp_path / "node_catalog.sqlite3", tmp_path / "hub_catalog.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")

    with (
        Ledger(node_db, telemetry=telemetry) as ledger,
        HubRollupIngestStore(hub_db) as ingest_store,
    ):
        _seed_hub_entity_catalog(ingest_store)
        act = ledger.create_activity("Implement WT-300", external_ref="WT-300")
        end_at = WORK_START + timedelta(seconds=ONE_HOUR_SECONDS)
        local_interval = ledger.record_structured_interval(
            act.id, started_at=WORK_START, ended_at=end_at, notes="Local-only interval note"
        )
        transport = hub_sync.LoopbackHubSyncTransport(ingest_store=ingest_store)
        client = hub_sync.EntityCatalogSyncClient(
            ledger=ledger,
            transport=transport,
            config=HubEndpointConfig(base_url=HUB_URL, node_id=NODE_ALPHA_ID),
            telemetry=telemetry,
            environ={"FRITZ_TRACE_ID": EXPECTED_TRACE_ID},
        )
        first_sync, second_sync = client.sync_catalog(), client.sync_catalog()
        assert (first_sync.entities_created, first_sync.attributions_upserted) == (4, 4)
        assert (second_sync.entities_created, second_sync.entities_unchanged) == (0, 4)
        assert (len(ledger.list_entities()), len(ledger.list_attributions())) == (4, 4)

        ingest_store.upsert_catalog_entity(
            core.ENTITY_KIND_CUSTOMER, "cust-acme", "ACME International"
        )
        assert client.pull_catalog().entities_updated == 1
        acme = ledger.find_entity(core.ENTITY_KIND_CUSTOMER, "cust-acme")
        assert acme is not None and acme.name == "ACME International"

        customer_shares = {
            share.entity.key: share.attributed_seconds
            for share in ledger.query_entity_attribution(target_kind=core.ENTITY_KIND_CUSTOMER)
        }
        assert customer_shares["cust-acme"] == pytest.approx(ONE_HOUR_SECONDS * WEIGHT_SIXTY)
        assert customer_shares["cust-globex"] == pytest.approx(ONE_HOUR_SECONDS * WEIGHT_FORTY)

        intervals_after = ledger.list_intervals()
        assert len(intervals_after) == 1 and intervals_after[0].id == local_interval.id
        assert intervals_after[0].notes == "Local-only interval note"
        assert (transport.pushed_rollups, len(transport.catalog_pulls)) == ([], 3)


def test_entity_catalog_sync_via_urllib_transport_and_missing_token_error(
    tmp_path: Path,
) -> None:
    """Verify `EntityCatalogSyncClient` over `UrllibHubSyncTransport` and missing token error."""
    node_db = tmp_path / "node_urllib_cat.sqlite3"
    token_store = _seed_token_store(tmp_path, token_secret="cat_pull_token_123")
    seen_urls: list[tuple[str, str, dict[str, str]]] = []
    apollo = hub_sync.CatalogEntityRecord(
        kind=core.ENTITY_KIND_PROJECT, key="apollo", name="Project Apollo"
    )
    catalog_payload = {
        "catalog_version": 2,
        "entities": [apollo.to_dict()],
        "attribution_templates": [],
    }

    def _catalog_urlopen(
        req: urllib.request.Request, timeout: float = 15.0
    ) -> _RecordingUrllibResponse:
        """Verify GET catalog request has no body and carries auth + trace headers."""
        _ = timeout
        assert req.data is None
        seen_urls.append((req.get_method(), req.full_url, dict(req.header_items())))
        return _RecordingUrllibResponse(status=HTTP_OK, payload=catalog_payload)

    config = HubEndpointConfig(base_url=HUB_URL, node_id=NODE_ALPHA_ID)
    env = {"FRITZ_TRACE_ID": EXPECTED_TRACE_ID}
    transport = hub_sync.UrllibHubSyncTransport(
        config=config, token_store=token_store, environ=env, urlopen=_catalog_urlopen
    )
    with Ledger(node_db) as ledger:
        client = hub_sync.EntityCatalogSyncClient(ledger=ledger, transport=transport, config=config)
        result = client.sync_catalog()
        assert (result.entities_created, result.catalog_version) == (1, 2)
        assert ledger.find_entity(core.ENTITY_KIND_PROJECT, "apollo") is not None

    method, full_url, hdrs = seen_urls[0]
    lower_hdrs = {k.lower(): v for k, v in hdrs.items()}
    assert (method, full_url) == ("GET", f"{HUB_URL}{hub_sync.DEFAULT_CATALOG_PULL_PATH}")
    assert lower_hdrs["authorization"] == "Bearer cat_pull_token_123"
    assert lower_hdrs[hub_sync.TRACE_ID_HEADER_NAME.lower()] == EXPECTED_TRACE_ID

    empty_store = UserTokenStore(
        keyring_backend=_InMemoryKeyring(), fallback_path=tmp_path / "empty_tokens.json"
    )
    unauth_transport = hub_sync.UrllibHubSyncTransport(
        config=config, token_store=empty_store, require_auth=True, urlopen=_catalog_urlopen
    )
    with pytest.raises(hub_sync.HubAuthenticationError):
        unauth_transport.pull_entity_catalog()


# endregion
