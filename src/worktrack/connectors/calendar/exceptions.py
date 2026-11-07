"""Scrubbing exception hierarchy for `worktrack.connectors.calendar`."""

from __future__ import annotations

from collections.abc import Sequence

from worktrack.connectors.exceptions import ConnectorError
from worktrack.secrets.exceptions import SecretValue


# region Exceptions
class CalendarConnectorError(ConnectorError):
    """Base exception for Calendar `Sources` (iCal, JMAP, MS 365) and `CalendarFocusSink`."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        destination_uri: str | None = None,
        redact: Sequence[str | SecretValue] = (),
    ) -> None:
        """Initialize calendar connector error with optional HTTP status and secret scrubbing."""
        super().__init__(message, redact=redact)
        self.http_status = http_status
        self.destination_uri = destination_uri


class CalendarConfigurationError(CalendarConnectorError):
    """Raised when calendar connector settings, URLs, or bucket mapping rules are invalid."""


class CalendarAuthenticationError(CalendarConnectorError):
    """Raised when calendar OAuth/Bearer tokens in `UserTokenStore` are missing or rejected."""


class CalendarTransportError(CalendarConnectorError):
    """Raised when calendar HTTP or file transport fails or returns malformed responses."""


class CalendarParseError(CalendarConnectorError):
    """Raised when iCal (RFC 5545), JMAP (RFC 8984), or MS 365 event payloads fail parsing."""


# endregion
