"""Exception hierarchy for `worktrack.outbox`."""

from __future__ import annotations

from collections.abc import Sequence

from worktrack.secrets.exceptions import SecretValue, scrub_secrets


class OutboxError(Exception):
    """Base exception for `worktrack.outbox` with automatic secret scrubbing."""

    def __init__(
        self,
        message: str,
        *,
        redact: Sequence[str | SecretValue] = (),
    ) -> None:
        """Initialize error with scrubbed diagnostic message."""
        super().__init__(scrub_secrets(message, redact=redact))


class OutboxValidationError(OutboxError):
    """Raised when a `RollupPolicy`, `OutboxItem`, or cadence parameter is invalid."""


class OutboxItemNotFoundError(OutboxError):
    """Raised when a requested `OutboxItem` identifier does not exist."""


class OutboxStateError(OutboxError):
    """Raised when an invalid `OutboxItem` status transition is requested."""


class OutboxMigrationError(OutboxError):
    """Raised when applying or verifying `outbox_schema_migrations` fails."""


class EgressBypassBlockedError(OutboxError):
    """Raised when any caller attempts to write to a `Sink` bypassing `Outbox` approval."""


class EgressLedgerImmutableError(OutboxError):
    """Raised when an attempt is made to mutate or delete append-only `EgressLedger` rows."""


class SinkDeliveryError(OutboxError):
    """Raised when an approved `Sink` delivery fails due to upstream HTTP or transport errors."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        destination_uri: str | None = None,
        redact: Sequence[str | SecretValue] = (),
    ) -> None:
        """Initialize sink delivery error with optional HTTP status and destination URI."""
        super().__init__(message, redact=redact)
        self.http_status = http_status
        self.destination_uri = destination_uri
