"""Sealed `ApprovedOutboxGrant` capability tokens and Outbox-bypass enforcement."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from worktrack.outbox.exceptions import EgressBypassBlockedError, OutboxValidationError
from worktrack.outbox.models import OutboxItem, SinkWriteReceipt
from worktrack.outbox.policies import compute_payload_sha256, validate_payload_hygiene
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
_GRANT_MINT_SENTINEL: Final[object] = object()
FALLBACK_BYPASS_LABEL: Final = "unknown"
SAFE_LABEL_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# endregion


# region Protocols
class RollupSink(Protocol):
    """Structural interface for external `Rollup` destination `Sinks`."""

    @property
    def sink_name(self) -> str:
        """Return bounded lowercase sink identifier (e.g. `'jira'`)."""
        ...

    def write_rollup(
        self,
        item: OutboxItem,
        grant: ApprovedOutboxGrant | None = None,
        *,
        source: str = "direct_call",
    ) -> SinkWriteReceipt:
        """Write an `ApprovedOutboxGrant`-verified `OutboxItem` to the external `Sink`."""
        ...


# endregion


# region Grants
@dataclass(frozen=True, slots=True)
class ApprovedOutboxGrant:
    """Sealed capability token minted exclusively by `OutboxStore` for approved items."""

    outbox_item_id: str
    idempotency_key: str
    sink: str
    payload_sha256: str
    issued_at: datetime
    _signature: str = field(repr=False)
    _live_check: Callable[[ApprovedOutboxGrant, OutboxItem], bool] = field(repr=False)
    _sentinel: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Forbid direct construction outside `mint_outbox_grant`."""
        if self._sentinel is not _GRANT_MINT_SENTINEL:
            raise EgressBypassBlockedError(
                "ApprovedOutboxGrant cannot be constructed directly; use OutboxStore.issue_grant."
            )

    def verify_against_store(self, item: OutboxItem) -> bool:
        """Verify HMAC authenticity and live `status == 'approved'` state in `OutboxStore`."""
        return self._live_check(self, item)


def mint_outbox_grant(
    *,
    item: OutboxItem,
    issued_at: datetime,
    secret_key: bytes,
    live_check: Callable[[ApprovedOutboxGrant, OutboxItem], bool],
) -> ApprovedOutboxGrant:
    """Mint a sealed `ApprovedOutboxGrant` signed with `OutboxStore`'s ephemeral key."""
    signature = compute_grant_hmac(
        secret_key=secret_key,
        outbox_item_id=item.id,
        idempotency_key=item.idempotency_key,
        sink=item.sink,
        payload_sha256=item.payload_sha256,
    )
    return ApprovedOutboxGrant(
        outbox_item_id=item.id,
        idempotency_key=item.idempotency_key,
        sink=item.sink,
        payload_sha256=item.payload_sha256,
        issued_at=issued_at,
        _signature=signature,
        _live_check=live_check,
        _sentinel=_GRANT_MINT_SENTINEL,
    )


def compute_grant_hmac(
    *,
    secret_key: bytes,
    outbox_item_id: str,
    idempotency_key: str,
    sink: str,
    payload_sha256: str,
) -> str:
    """Compute HMAC-SHA256 tag binding grant fields to the issuing `OutboxStore`."""
    msg = f"{outbox_item_id}|{idempotency_key}|{sink}|{payload_sha256}".encode()
    return hmac.new(secret_key, msg, "sha256").hexdigest()


def verify_grant_signature(grant: ApprovedOutboxGrant, secret_key: bytes) -> bool:
    """Compare `grant`'s embedded HMAC tag in constant time."""
    expected = compute_grant_hmac(
        secret_key=secret_key,
        outbox_item_id=grant.outbox_item_id,
        idempotency_key=grant.idempotency_key,
        sink=grant.sink,
        payload_sha256=grant.payload_sha256,
    )
    return hmac.compare_digest(grant._signature, expected)


def enforce_outbox_grant(
    *,
    sink_name: str,
    item: object,
    grant: ApprovedOutboxGrant | None,
    telemetry: TelemetryRecorder | None = None,
    source: str = "direct_call",
) -> None:
    """Verify `item` and `grant` before any outbound `Sink` network call.

    Increments `worktrack_egress_bypass_blocked_total{sink,source}` and raises
    `EgressBypassBlockedError` whenever `grant` is missing, forged, revoked, or
    paired with a mutated or non-hygienic payload.
    """
    if not isinstance(item, OutboxItem):
        _block_egress(sink_name, source, telemetry, "Outbound payload must be an OutboxItem.")
    assert isinstance(item, OutboxItem)
    try:
        validate_payload_hygiene(item.payload_preview)
        actual_sha = compute_payload_sha256(item.payload_preview)
    except OutboxValidationError as exc:
        _block_egress(sink_name, source, telemetry, str(exc))
        raise AssertionError from exc

    if actual_sha != item.payload_sha256:
        _block_egress(sink_name, source, telemetry, "OutboxItem payload_preview was tampered with.")
    if not isinstance(grant, ApprovedOutboxGrant):
        _block_egress(sink_name, source, telemetry, "Missing ApprovedOutboxGrant for Sink write.")
    assert isinstance(grant, ApprovedOutboxGrant)

    if (
        grant.sink != sink_name
        or grant.outbox_item_id != item.id
        or grant.idempotency_key != item.idempotency_key
        or grant.payload_sha256 != actual_sha
        or not grant.verify_against_store(item)
    ):
        _block_egress(
            sink_name,
            source,
            telemetry,
            f"ApprovedOutboxGrant verification failed for OutboxItem '{item.id}'.",
        )


def sanitize_bypass_label(raw_value: str) -> str:
    """Normalize a sink or source label into a safe bounded Prometheus slug."""
    candidate = raw_value.strip().lower()
    if SAFE_LABEL_PATTERN.match(candidate):
        return candidate
    return FALLBACK_BYPASS_LABEL


def _block_egress(
    sink_name: str,
    source: str,
    telemetry: TelemetryRecorder | None,
    reason: str,
) -> None:
    """Record `worktrack_egress_bypass_blocked_total` and raise `EgressBypassBlockedError`."""
    if telemetry is not None:
        telemetry.record_egress_bypass_blocked(
            sink=sanitize_bypass_label(sink_name),
            source=sanitize_bypass_label(source),
        )
    raise EgressBypassBlockedError(reason)


# endregion
