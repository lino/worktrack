"""SQLite `OutboxStore` for staging, approval transitions, and append-only `EgressLedger`."""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from worktrack.config import WorktrackSettings
from worktrack.core.models import MigrationRecord, format_utc_iso
from worktrack.core.schema import (
    DEFAULT_DB_DIR_MODE,
    open_sqlite_connection,
    prepare_db_target,
    resolve_busy_timeout_ms,
    transaction,
)
from worktrack.outbox import egress_ops
from worktrack.outbox.exceptions import (
    EgressBypassBlockedError,
    OutboxError,
    OutboxItemNotFoundError,
    OutboxStateError,
)
from worktrack.outbox.grants import (
    ApprovedOutboxGrant,
    enforce_outbox_grant,
    mint_outbox_grant,
    sanitize_bypass_label,
    verify_grant_signature,
)
from worktrack.outbox.models import (
    OUTBOX_STATUS_APPROVED,
    OUTBOX_STATUS_FAILED,
    OUTBOX_STATUS_REJECTED,
    OUTBOX_STATUS_STAGED,
    EgressRecord,
    OutboxItem,
    RollupProposal,
)
from worktrack.outbox.policies import (
    canonical_payload_json,
    compute_payload_sha256,
    validate_payload_hygiene,
)
from worktrack.outbox.schema import apply_outbox_migrations, list_applied_outbox_migrations
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
EMPTY_SCHEMA_VERSION: Final = 0
GRANT_SECRET_BYTES: Final = 32
APPROVABLE_SOURCE_STATUSES: Final[frozenset[str]] = frozenset(
    {OUTBOX_STATUS_STAGED, OUTBOX_STATUS_APPROVED, OUTBOX_STATUS_FAILED}
)
REJECTABLE_SOURCE_STATUSES: Final[frozenset[str]] = frozenset(
    {OUTBOX_STATUS_STAGED, OUTBOX_STATUS_APPROVED, OUTBOX_STATUS_FAILED, OUTBOX_STATUS_REJECTED}
)
# endregion


# region Outbox Store
class OutboxStore:
    """SQLite store managing previewable `OutboxItems`, grants, and append-only `EgressLedger`."""

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
        """Open or attach SQLite connection and apply `outbox_schema_migrations`."""
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
            apply_outbox_migrations(conn)
        except Exception:
            if owns_conn:
                conn.close()
            raise

        self._conn = conn
        self._owns_conn = owns_conn
        self._db_path = resolved_path
        self._telemetry = telemetry
        self._grant_secret = secrets.token_bytes(GRANT_SECRET_BYTES)
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
        """Construct an `OutboxStore` bound to `settings.db_path`."""
        return cls(
            settings.db_path,
            telemetry=telemetry,
            busy_timeout_ms=busy_timeout_ms,
            environ=environ,
        )

    @property
    def outbox_schema_version(self) -> int:
        """Return highest applied `outbox_schema_migrations` version."""
        row = (
            self._require_conn()
            .execute("SELECT COALESCE(MAX(version), 0) FROM outbox_schema_migrations")
            .fetchone()
        )
        return EMPTY_SCHEMA_VERSION if row is None else int(row[0])

    def applied_migrations(self) -> list[MigrationRecord]:
        """List applied `outbox_schema_migrations` records in ascending order."""
        return list_applied_outbox_migrations(self._require_conn())

    def close(self) -> None:
        """Close owned SQLite connection idempotently."""
        if not self._closed:
            if self._owns_conn:
                self._conn.close()
            self._closed = True

    def __enter__(self) -> Self:
        """Enter context manager returning this `OutboxStore`."""
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

    def stage_proposal(self, proposal: RollupProposal) -> OutboxItem:
        """Stage a `RollupProposal` idempotently into `outbox_items`."""
        validate_payload_hygiene(proposal.payload_preview)
        conn = self._require_conn()
        existing = self.find_active_by_idempotency_key(proposal.idempotency_key)
        if existing is not None:
            return existing
        item_id = str(uuid.uuid4())
        now_iso = format_utc_iso(datetime.now(UTC))
        payload_json = canonical_payload_json(proposal.payload_preview)
        payload_sha = compute_payload_sha256(proposal.payload_preview)
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO outbox_items (
                    id, idempotency_key, sink, target_ref, window_start, window_end,
                    raw_duration_seconds, rounded_duration_seconds, summary, cadence, status,
                    source_interval_ids_json, payload_preview_json, payload_sha256,
                    attempt_count, rejection_reason, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)
                """,
                (
                    item_id,
                    proposal.idempotency_key,
                    proposal.sink,
                    proposal.target_ref,
                    format_utc_iso(proposal.window_start),
                    format_utc_iso(proposal.window_end),
                    proposal.raw_duration_seconds,
                    proposal.rounded_duration_seconds,
                    proposal.summary,
                    proposal.cadence,
                    OUTBOX_STATUS_STAGED,
                    json.dumps(list(proposal.source_interval_ids)),
                    payload_json,
                    payload_sha,
                    now_iso,
                    now_iso,
                ),
            )
        return self.get_item(item_id)

    def stage_proposals(self, proposals: Sequence[RollupProposal]) -> list[OutboxItem]:
        """Stage a sequence of `RollupProposals` and return persisted `OutboxItems`."""
        return [self.stage_proposal(prop) for prop in proposals]

    def get_item(self, item_id: str) -> OutboxItem:
        """Fetch an `OutboxItem` by primary identifier."""
        row = (
            self._require_conn()
            .execute("SELECT * FROM outbox_items WHERE id = ?", (item_id.strip(),))
            .fetchone()
        )
        if row is None:
            raise OutboxItemNotFoundError(f"OutboxItem '{item_id}' does not exist.")
        return egress_ops.row_to_outbox_item(row)

    def find_active_by_idempotency_key(self, idempotency_key: str) -> OutboxItem | None:
        """Find a `staged` or `approved` `OutboxItem` matching `idempotency_key`."""
        row = (
            self._require_conn()
            .execute(
                """
                SELECT * FROM outbox_items
                WHERE idempotency_key = ? AND status IN ('staged', 'approved')
                LIMIT 1
                """,
                (idempotency_key.strip(),),
            )
            .fetchone()
        )
        return None if row is None else egress_ops.row_to_outbox_item(row)

    def list_items(
        self,
        *,
        status: str | None = None,
        sink: str | None = None,
        cadence: str | None = None,
    ) -> list[OutboxItem]:
        """List `OutboxItems` filtered by optional `status`, `sink`, and `cadence`."""
        clauses, params = egress_ops.build_item_filters(
            status=status,
            sink=sink,
            cadence=cadence,
        )
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = (
            self._require_conn()
            .execute(
                f"SELECT * FROM outbox_items {where_sql} ORDER BY window_start ASC, created_at ASC",
                params,
            )
            .fetchall()
        )
        return [egress_ops.row_to_outbox_item(row) for row in rows]

    def approve_item(self, item_id: str) -> OutboxItem:
        """Mark a `staged` or `failed` `OutboxItem` as `approved` for egress."""
        conn = self._require_conn()
        with transaction(conn):
            item = self.get_item(item_id)
            if item.status not in APPROVABLE_SOURCE_STATUSES:
                raise OutboxStateError(f"Cannot approve OutboxItem in status '{item.status}'.")
            now_iso = format_utc_iso(datetime.now(UTC))
            conn.execute(
                """
                UPDATE outbox_items
                SET status = ?, rejection_reason = NULL, updated_at = ?
                WHERE id = ?
                """,
                (OUTBOX_STATUS_APPROVED, now_iso, item.id),
            )
        return self.get_item(item_id)

    def reject_item(self, item_id: str, *, reason: str | None = None) -> OutboxItem:
        """Reject a `staged`, `approved`, or `failed` `OutboxItem`."""
        conn = self._require_conn()
        with transaction(conn):
            item = self.get_item(item_id)
            if item.status not in REJECTABLE_SOURCE_STATUSES:
                raise OutboxStateError(f"Cannot reject OutboxItem in status '{item.status}'.")
            now_iso = format_utc_iso(datetime.now(UTC))
            conn.execute(
                """
                UPDATE outbox_items
                SET status = ?, rejection_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (OUTBOX_STATUS_REJECTED, reason, now_iso, item.id),
            )
        return self.get_item(item_id)

    def issue_grant(self, item_id: str, *, source: str = "outbox_store") -> ApprovedOutboxGrant:
        """Issue a sealed `ApprovedOutboxGrant` strictly for `status == 'approved'` items."""
        item = self.get_item(item_id)
        if item.status != OUTBOX_STATUS_APPROVED:
            if self._telemetry is not None:
                self._telemetry.record_egress_bypass_blocked(
                    sink=sanitize_bypass_label(item.sink),
                    source=sanitize_bypass_label(source),
                )
            raise EgressBypassBlockedError(
                f"Cannot issue grant for OutboxItem '{item.id}' with status '{item.status}'."
            )
        validate_payload_hygiene(item.payload_preview)
        return mint_outbox_grant(
            item=item,
            issued_at=datetime.now(UTC),
            secret_key=self._grant_secret,
            live_check=self._verify_live_grant,
        )

    def verify_grant(
        self,
        item: OutboxItem,
        grant: ApprovedOutboxGrant | None,
        *,
        expected_sink: str,
        source: str = "direct_call",
    ) -> None:
        """Verify `grant` against `item` and live `OutboxStore` state or block egress."""
        enforce_outbox_grant(
            sink_name=expected_sink,
            item=item,
            grant=grant,
            telemetry=self._telemetry,
            source=source,
        )

    def has_flushed_idempotency_key(self, sink: str, idempotency_key: str) -> EgressRecord | None:
        """Return prior `'ok'` `EgressRecord` if `(sink, idempotency_key)` was already flushed."""
        return egress_ops.query_flushed_idempotency_key(
            self._require_conn(),
            sink,
            idempotency_key,
        )

    def list_flushed_bucket_baselines(self, sink: str) -> dict[tuple[str, str, str], OutboxItem]:
        """Load latest flushed `OutboxItem` per `(target_ref, window_start, window_end)` bucket."""
        return egress_ops.query_flushed_bucket_baselines(self._require_conn(), sink)

    def finalize_flush_attempt(
        self,
        *,
        item: OutboxItem,
        next_status: str,
        increment_attempt: bool,
        outcome: str,
        destination_uri: str,
        http_status: int | None,
        external_receipt_id: str | None,
        trace_id: str,
        error_detail: str | None = None,
    ) -> tuple[OutboxItem, EgressRecord]:
        """Atomically update `outbox_items` status and append an immutable `EgressRecord`."""
        record = egress_ops.persist_flush_attempt(
            self._require_conn(),
            item=item,
            next_status=next_status,
            increment_attempt=increment_attempt,
            outcome=outcome,
            destination_uri=destination_uri,
            http_status=http_status,
            external_receipt_id=external_receipt_id,
            trace_id=trace_id,
            error_detail=error_detail,
        )
        return self.get_item(item.id), record

    def list_egress_records(
        self,
        *,
        sink: str | None = None,
        outcome: str | None = None,
        outbox_item_id: str | None = None,
    ) -> list[EgressRecord]:
        """Query append-only `EgressLedger` receipts in chronological order."""
        return egress_ops.query_egress_records(
            self._require_conn(),
            sink=sink,
            outcome=outcome,
            outbox_item_id=outbox_item_id,
        )

    def _verify_live_grant(self, grant: ApprovedOutboxGrant, item: OutboxItem) -> bool:
        """Check HMAC signature and confirm `item.id` remains `approved` in SQLite."""
        if self._closed or not verify_grant_signature(grant, self._grant_secret):
            return False
        try:
            persisted = self.get_item(item.id)
        except OutboxItemNotFoundError:
            return False
        return (
            persisted.status == OUTBOX_STATUS_APPROVED
            and persisted.payload_sha256 == grant.payload_sha256
            and persisted.idempotency_key == grant.idempotency_key
        )

    def _require_conn(self) -> sqlite3.Connection:
        """Return open SQLite connection or raise `OutboxError`."""
        if self._closed:
            raise OutboxError("OutboxStore SQLite connection is closed.")
        return self._conn


# endregion
