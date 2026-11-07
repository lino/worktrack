"""SQLite row hydration and append-only `EgressLedger` persistence helpers."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from worktrack.core.models import format_utc_iso, parse_utc_iso
from worktrack.core.schema import transaction
from worktrack.outbox.exceptions import OutboxValidationError
from worktrack.outbox.models import (
    ALLOWED_EGRESS_OUTCOMES,
    ALLOWED_OUTBOX_STATUSES,
    EGRESS_OUTCOME_OK,
    OUTBOX_STATUS_FLUSHED,
    EgressRecord,
    OutboxItem,
)


def build_item_filters(
    *,
    status: str | None,
    sink: str | None,
    cadence: str | None,
) -> tuple[list[str], list[object]]:
    """Build SQL WHERE clauses and parameters for `OutboxStore.list_items`."""
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        if status not in ALLOWED_OUTBOX_STATUSES:
            raise OutboxValidationError(f"Unknown outbox status '{status}'.")
        clauses.append("status = ?")
        params.append(status)
    if sink is not None:
        clauses.append("sink = ?")
        params.append(sink.strip())
    if cadence is not None:
        clauses.append("cadence = ?")
        params.append(cadence.strip())
    return clauses, params


def row_to_outbox_item(row: sqlite3.Row) -> OutboxItem:
    """Hydrate an `OutboxItem` dataclass from a SQLite row."""
    raw_reason = row["rejection_reason"]
    raw_err = row["last_error"]
    return OutboxItem(
        id=str(row["id"]),
        idempotency_key=str(row["idempotency_key"]),
        sink=str(row["sink"]),
        target_ref=str(row["target_ref"]),
        window_start=parse_utc_iso(str(row["window_start"])),
        window_end=parse_utc_iso(str(row["window_end"])),
        raw_duration_seconds=float(row["raw_duration_seconds"]),
        rounded_duration_seconds=int(row["rounded_duration_seconds"]),
        summary=str(row["summary"]),
        cadence=str(row["cadence"]),
        status=str(row["status"]),
        source_interval_ids=tuple(json.loads(str(row["source_interval_ids_json"]))),
        payload_preview=dict(json.loads(str(row["payload_preview_json"]))),
        payload_sha256=str(row["payload_sha256"]),
        attempt_count=int(row["attempt_count"]),
        rejection_reason=None if raw_reason is None else str(raw_reason),
        last_error=None if raw_err is None else str(raw_err),
        created_at=parse_utc_iso(str(row["created_at"])),
        updated_at=parse_utc_iso(str(row["updated_at"])),
    )


def row_to_egress_record(row: sqlite3.Row) -> EgressRecord:
    """Hydrate an `EgressRecord` dataclass from an `egress_ledger` SQLite row."""
    raw_status = row["http_status"]
    raw_receipt = row["external_receipt_id"]
    raw_err = row["error_detail"]
    return EgressRecord(
        id=str(row["id"]),
        recorded_at=parse_utc_iso(str(row["recorded_at"])),
        outbox_item_id=str(row["outbox_item_id"]),
        idempotency_key=str(row["idempotency_key"]),
        sink=str(row["sink"]),
        destination_uri=str(row["destination_uri"]),
        payload_sha256=str(row["payload_sha256"]),
        outcome=str(row["outcome"]),
        http_status=None if raw_status is None else int(raw_status),
        external_receipt_id=None if raw_receipt is None else str(raw_receipt),
        trace_id=str(row["trace_id"]),
        error_detail=None if raw_err is None else str(raw_err),
    )


def query_flushed_idempotency_key(
    conn: sqlite3.Connection,
    sink: str,
    idempotency_key: str,
) -> EgressRecord | None:
    """Return prior `'ok'` `EgressRecord` if `(sink, idempotency_key)` was already flushed."""
    row = conn.execute(
        """
        SELECT * FROM egress_ledger
        WHERE sink = ? AND idempotency_key = ? AND outcome = ?
        LIMIT 1
        """,
        (sink.strip(), idempotency_key.strip(), EGRESS_OUTCOME_OK),
    ).fetchone()
    return None if row is None else row_to_egress_record(row)


def query_flushed_bucket_baselines(
    conn: sqlite3.Connection,
    sink: str,
) -> dict[tuple[str, str, str], OutboxItem]:
    """Load latest flushed `OutboxItem` per `(target_ref, window_start, window_end)` bucket."""
    rows = conn.execute(
        """
        SELECT * FROM outbox_items
        WHERE sink = ? AND status = ?
        ORDER BY updated_at ASC, created_at ASC
        """,
        (sink.strip(), OUTBOX_STATUS_FLUSHED),
    ).fetchall()
    baselines: dict[tuple[str, str, str], OutboxItem] = {}
    for row in rows:
        item = row_to_outbox_item(row)
        key = (
            item.target_ref,
            format_utc_iso(item.window_start),
            format_utc_iso(item.window_end),
        )
        baselines[key] = item
    return baselines


def persist_flush_attempt(
    conn: sqlite3.Connection,
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
) -> EgressRecord:
    """Atomically update `outbox_items` status and append an immutable `EgressRecord`."""
    if outcome not in ALLOWED_EGRESS_OUTCOMES:
        raise OutboxValidationError(f"Unsupported egress outcome '{outcome}'.")
    record_id = str(uuid.uuid4())
    now_iso = format_utc_iso(datetime.now(UTC))
    next_attempts = item.attempt_count + (1 if increment_attempt else 0)
    with transaction(conn):
        conn.execute(
            """
            UPDATE outbox_items
            SET status = ?, attempt_count = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (next_status, next_attempts, error_detail, now_iso, item.id),
        )
        conn.execute(
            """
            INSERT INTO egress_ledger (
                id, recorded_at, outbox_item_id, idempotency_key, sink,
                destination_uri, payload_sha256, outcome, http_status,
                external_receipt_id, trace_id, error_detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                now_iso,
                item.id,
                item.idempotency_key,
                item.sink,
                destination_uri,
                item.payload_sha256,
                outcome,
                http_status,
                external_receipt_id,
                trace_id,
                error_detail,
            ),
        )
    row = conn.execute("SELECT * FROM egress_ledger WHERE id = ?", (record_id,)).fetchone()
    assert row is not None
    return row_to_egress_record(row)


def query_egress_records(
    conn: sqlite3.Connection,
    *,
    sink: str | None = None,
    outcome: str | None = None,
    outbox_item_id: str | None = None,
) -> list[EgressRecord]:
    """Query append-only `EgressLedger` receipts in chronological order."""
    clauses: list[str] = []
    params: list[object] = []
    if sink is not None:
        clauses.append("sink = ?")
        params.append(sink.strip())
    if outcome is not None:
        clauses.append("outcome = ?")
        params.append(outcome.strip())
    if outbox_item_id is not None:
        clauses.append("outbox_item_id = ?")
        params.append(outbox_item_id.strip())
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM egress_ledger {where_sql} ORDER BY recorded_at ASC, rowid ASC",
        params,
    ).fetchall()
    return [row_to_egress_record(row) for row in rows]
