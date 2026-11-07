"""Connection base and Entity/Attribution/Facet mixin methods for `Ledger`."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from worktrack.core import (
    attribution as attribution_ops,
)
from worktrack.core import (
    entities as entity_ops,
)
from worktrack.core import (
    facets as facet_ops,
)
from worktrack.core.exceptions import LedgerError
from worktrack.core.models import (
    DEFAULT_CONTEXT_FACT_SOURCE,
    Attribution,
    ContextFact,
    Entity,
    EntityAttributionShare,
    Facet,
    FacetValue,
    MigrationRecord,
)
from worktrack.core.schema import (
    DEFAULT_DB_DIR_MODE,
    apply_migrations,
    list_applied_migrations,
    open_sqlite_connection,
    prepare_db_target,
    resolve_busy_timeout_ms,
)
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
METRIC_ATTRIBUTION_QUERIES_TOTAL: Final = "worktrack_attribution_queries_total"
METRIC_CONTEXT_FACTS_ASSERTED_TOTAL: Final = "worktrack_context_facts_asserted_total"
EMPTY_SCHEMA_VERSION: Final = 0
# endregion


# region Lifecycle
class LedgerConnectionBase:
    """SQLite WAL connection lifecycle and migration state shared by `Ledger`."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        telemetry: TelemetryRecorder | None = None,
        busy_timeout_ms: int | None = None,
        dir_mode: int = DEFAULT_DB_DIR_MODE,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Open SQLite WAL ledger, run deterministic migrations, and seed facets."""
        resolved_timeout = resolve_busy_timeout_ms(busy_timeout_ms, environ=environ)
        connect_target, resolved_path = prepare_db_target(db_path, dir_mode=dir_mode)
        conn = open_sqlite_connection(connect_target, busy_timeout_ms=resolved_timeout)
        try:
            apply_migrations(conn)
        except Exception:
            conn.close()
            raise
        self._conn: sqlite3.Connection = conn
        self._db_path: Path | None = resolved_path
        self._busy_timeout_ms: int = resolved_timeout
        self._telemetry: TelemetryRecorder | None = telemetry
        self._closed: bool = False

    @property
    def db_path(self) -> Path | None:
        """Return resolved SQLite file `Path` (or `None` for `:memory:`)."""
        return self._db_path

    @property
    def busy_timeout_ms(self) -> int:
        """Return configured SQLite `PRAGMA busy_timeout` in milliseconds."""
        row = self._require_conn().execute("PRAGMA busy_timeout").fetchone()
        return self._busy_timeout_ms if row is None else int(row[0])

    @property
    def journal_mode(self) -> str:
        """Return active SQLite `PRAGMA journal_mode`."""
        row = self._require_conn().execute("PRAGMA journal_mode").fetchone()
        return "" if row is None else str(row[0])

    @property
    def foreign_keys_enabled(self) -> bool:
        """Return `True` when `PRAGMA foreign_keys` is enabled on the connection."""
        row = self._require_conn().execute("PRAGMA foreign_keys").fetchone()
        return False if row is None else bool(row[0])

    @property
    def schema_version(self) -> int:
        """Return highest applied schema migration version."""
        row = (
            self._require_conn()
            .execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
            .fetchone()
        )
        return EMPTY_SCHEMA_VERSION if row is None else int(row[0])

    def applied_migrations(self) -> list[MigrationRecord]:
        """List all applied SQLite schema migrations in ascending order."""
        return list_applied_migrations(self._require_conn())

    def close(self) -> None:
        """Close the underlying SQLite connection idempotently."""
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self) -> Self:
        """Enter context manager returning this `Ledger`."""
        self._require_conn()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the SQLite connection on context exit."""
        self.close()

    def _require_conn(self) -> sqlite3.Connection:
        """Return active SQLite connection or raise `LedgerError` if closed."""
        if self._closed:
            raise LedgerError("Ledger SQLite connection is closed.")
        return self._conn


# endregion


# region Attribution
class EntityAndFacetLedgerMixin(LedgerConnectionBase):
    """`Entity`, `Attribution`, `Facet`, and `ContextFact` facade operations."""

    def create_entity(
        self,
        kind: str,
        key: str,
        name: str,
        *,
        metadata: Mapping[str, object] | None = None,
        entity_id: str | None = None,
    ) -> Entity:
        """Create a non-strict `Entity` node."""
        return entity_ops.create_entity(
            self._require_conn(), kind, key, name, metadata=metadata, entity_id=entity_id
        )

    def get_entity(self, entity_id: str) -> Entity:
        """Fetch an `Entity` by identifier."""
        return entity_ops.get_entity(self._require_conn(), entity_id)

    def find_entity(self, kind: str, key: str) -> Entity | None:
        """Look up an `Entity` by `(kind, key)`."""
        return entity_ops.find_entity(self._require_conn(), kind, key)

    def list_entities(self, *, kind: str | None = None) -> list[Entity]:
        """List `Entities` optionally filtered by `kind`."""
        return entity_ops.list_entities(self._require_conn(), kind=kind)

    def set_attribution(
        self,
        *,
        target_entity_id: str,
        weight: float,
        source_activity_id: str | None = None,
        source_entity_id: str | None = None,
        enforce_weight_sum: bool = False,
    ) -> Attribution:
        """Upsert a cycle-checked weighted `Attribution` edge."""
        return entity_ops.set_attribution(
            self._require_conn(),
            target_entity_id=target_entity_id,
            weight=weight,
            source_activity_id=source_activity_id,
            source_entity_id=source_entity_id,
            enforce_weight_sum=enforce_weight_sum,
        )

    def remove_attribution(self, attribution_id: str) -> None:
        """Remove an `Attribution` edge by identifier."""
        entity_ops.remove_attribution(self._require_conn(), attribution_id)

    def list_attributions(
        self,
        *,
        source_activity_id: str | None = None,
        source_entity_id: str | None = None,
        target_entity_id: str | None = None,
    ) -> list[Attribution]:
        """List `Attribution` edges filtered by optional source or target IDs."""
        return entity_ops.list_attributions(
            self._require_conn(),
            source_activity_id=source_activity_id,
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
        )

    def validate_outgoing_weights(
        self,
        *,
        source_activity_id: str | None = None,
        source_entity_id: str | None = None,
    ) -> dict[str, float]:
        """Return outgoing `Attribution` weight sums grouped by target `Entity.kind`."""
        return entity_ops.validate_outgoing_weights(
            self._require_conn(),
            source_activity_id=source_activity_id,
            source_entity_id=source_entity_id,
        )

    def query_entity_attribution(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        target_kind: str | None = None,
        normalize: bool = False,
    ) -> list[EntityAttributionShare]:
        """Aggregate `EntityAttributionShare` across the DAG and record query metric."""
        shares = attribution_ops.query_entity_attribution(
            self._require_conn(),
            since=since,
            until=until,
            target_kind=target_kind,
            normalize=normalize,
        )
        if self._telemetry is not None:
            self._telemetry.increment_counter(METRIC_ATTRIBUTION_QUERIES_TOTAL)
        return shares

    def register_facet(
        self,
        key: str,
        value_type: str,
        *,
        allowed_values: Sequence[str] = (),
        description: str = "",
    ) -> Facet:
        """Register a custom typed `Facet` schema."""
        return facet_ops.register_facet(
            self._require_conn(),
            key,
            value_type,
            allowed_values=allowed_values,
            description=description,
        )

    def get_facet(self, key: str) -> Facet:
        """Fetch a registered `Facet` schema by key."""
        return facet_ops.get_facet(self._require_conn(), key)

    def list_facets(self) -> list[Facet]:
        """List all registered `Facet` schemas."""
        return facet_ops.list_facets(self._require_conn())

    def assert_context_fact(
        self,
        facet_key: str,
        value: FacetValue,
        *,
        scope_date: str | date | None = None,
        interval_id: str | None = None,
        source: str = DEFAULT_CONTEXT_FACT_SOURCE,
    ) -> ContextFact:
        """Assert/upsert a `ContextFact` and increment `context_facts_asserted_total`."""
        fact = facet_ops.assert_context_fact(
            self._require_conn(),
            facet_key,
            value,
            scope_date=scope_date,
            interval_id=interval_id,
            source=source,
        )
        if self._telemetry is not None:
            self._telemetry.increment_counter(METRIC_CONTEXT_FACTS_ASSERTED_TOTAL)
        return fact

    def get_context_fact(
        self,
        facet_key: str,
        *,
        scope_date: str | date | None = None,
        interval_id: str | None = None,
    ) -> ContextFact | None:
        """Look up a single `ContextFact` by `facet_key` and scope."""
        return facet_ops.get_context_fact(
            self._require_conn(), facet_key, scope_date=scope_date, interval_id=interval_id
        )

    def list_context_facts(
        self,
        *,
        facet_key: str | None = None,
        since_date: str | date | None = None,
        until_date: str | date | None = None,
        scope_date: str | date | None = None,
        interval_id: str | None = None,
    ) -> list[ContextFact]:
        """Query `ContextFacts` by optional facet key, date range, or interval ID."""
        return facet_ops.list_context_facts(
            self._require_conn(),
            facet_key=facet_key,
            since_date=since_date,
            until_date=until_date,
            scope_date=scope_date,
            interval_id=interval_id,
        )


# endregion
