"""Scrubbing exception hierarchy for `worktrack.connectors`."""

from __future__ import annotations

from collections.abc import Sequence

from worktrack.secrets.exceptions import SecretValue, scrub_secrets


class ConnectorError(Exception):
    """Base connector exception that scrubs credentials and tokens from diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        redact: Sequence[str | SecretValue] = (),
    ) -> None:
        """Initialize connector error with scrubbed message."""
        super().__init__(scrub_secrets(message, redact=redact))


class JiraConnectorError(ConnectorError):
    """Base exception for Jira Cloud OAuth, JQL `Source`, and Worklog `Sink` errors."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        destination_uri: str | None = None,
        redact: Sequence[str | SecretValue] = (),
    ) -> None:
        """Initialize Jira error with optional HTTP status, URI, and secret scrubbing."""
        super().__init__(message, redact=redact)
        self.http_status = http_status
        self.destination_uri = destination_uri


class JiraConfigurationError(JiraConnectorError):
    """Raised when `JiraCloudConfig` URL, cloud ID, or timeout parameters are invalid."""


class JiraAuthenticationError(JiraConnectorError):
    """Raised when local user OAuth tokens are missing, expired without refresh, or rejected."""


class JiraTransportError(JiraConnectorError):
    """Raised when network transport to Jira Cloud fails or returns malformed payloads."""


class JiraApiError(JiraConnectorError):
    """Raised when Jira Cloud REST API v3 returns a non-2xx error response."""
