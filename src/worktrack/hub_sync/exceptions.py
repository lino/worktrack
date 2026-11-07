"""Exception hierarchy for `worktrack.hub_sync` with automatic secret scrubbing."""

from __future__ import annotations

from collections.abc import Sequence

from worktrack.outbox.exceptions import (
    EgressBypassBlockedError,
    OutboxError,
    OutboxValidationError,
    SinkDeliveryError,
)
from worktrack.secrets.exceptions import SecretValue


class HubSyncError(OutboxError):
    """Base exception for `worktrack.hub_sync` with secret scrubbing and HTTP metadata."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        destination_uri: str | None = None,
        redact: Sequence[str | SecretValue] = (),
    ) -> None:
        """Initialize Hub sync error with optional HTTP status, URI, and redaction."""
        super().__init__(message, redact=redact)
        self.http_status = http_status
        self.destination_uri = destination_uri


class HubConfigurationError(HubSyncError):
    """Raised when `HubEndpointConfig` URL, node ID, or timeout values are invalid."""


class HubAuthenticationError(HubSyncError):
    """Raised when local user OAuth tokens are missing, expired, or rejected by the Hub."""


class HubTransportError(HubSyncError, SinkDeliveryError):
    """Raised when HTTP or loopback transport to the Hub fails or returns malformed data."""


class HubMigrationError(HubSyncError):
    """Raised when applying or verifying `hub_sync_schema_migrations` fails."""


class HubRollupValidationError(HubSyncError, OutboxValidationError):
    """Raised when a Ganglia-style rollup payload fails aggregate schema or SHA-256 checks."""


class HubEgressBypassError(EgressBypassBlockedError, HubRollupValidationError):
    """Raised when an outbound `GangliaHubSink` call lacks a valid grant or leaks raw keys."""
