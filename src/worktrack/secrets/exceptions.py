"""Redacted secret wrapper and scrubbing exception hierarchy for `worktrack.secrets`."""

from __future__ import annotations

import hmac
import re
from collections.abc import Iterable, Sequence
from typing import NoReturn

# region Constants
REDACTED_PLACEHOLDER = "***REDACTED***"
SECRET_REPR = f"SecretValue('{REDACTED_PLACEHOLDER}')"

CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(client_secret|access_token|refresh_token|code_verifier|password|secret)"
    r"(\s*[=:]\s*)([^\s&\"',;}\]]+)"
)
BEARER_HEADER_PATTERN = re.compile(r"(?i)\b(Bearer)(\s+)([A-Za-z0-9\-._~+/]+=*)")
# endregion


# region Models
class SecretValue:
    """Immutable secret string wrapper that always redacts formatting output.

    Plaintext is accessible solely via explicit `.expose()` calls so accidental
    string interpolation, `repr()`, `dataclasses.asdict()`, or traceback
    rendering never leaks confidential material.
    """

    __slots__ = ("_secret",)
    _secret: str

    def __init__(self, value: str) -> None:
        """Initialize an immutable `SecretValue` from a raw plaintext string."""
        if not isinstance(value, str):
            raise TypeError("SecretValue requires a string value.")
        object.__setattr__(self, "_secret", value)

    def expose(self) -> str:
        """Return the underlying plaintext secret string explicitly."""
        return self._secret

    def __repr__(self) -> str:
        """Return constant redacted developer representation."""
        return SECRET_REPR

    def __str__(self) -> str:
        """Return constant redacted user-facing string representation."""
        return SECRET_REPR

    def __format__(self, format_spec: str) -> str:
        """Format the redacted representation without exposing plaintext."""
        return format(SECRET_REPR, format_spec)

    def __eq__(self, other: object) -> bool:
        """Compare two `SecretValue` instances in constant time."""
        if isinstance(other, SecretValue):
            return hmac.compare_digest(self._secret, other._secret)
        return NotImplemented

    def __hash__(self) -> int:
        """Return hash of wrapped secret for immutable value semantics."""
        return hash(self._secret)

    def __copy__(self) -> SecretValue:
        """Return immutable `self` on shallow copy without exposing `_secret`."""
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> SecretValue:
        """Return immutable `self` on deep copy (`dataclasses.asdict` safe)."""
        return self

    def __getstate__(self) -> NoReturn:
        """Forbid pickling `SecretValue` to prevent plaintext byte serialization."""
        raise TypeError("SecretValue cannot be pickled.")

    def __setattr__(self, name: str, value: object) -> NoReturn:
        """Forbid attribute assignment on immutable `SecretValue`."""
        raise AttributeError(f"SecretValue is immutable; cannot set '{name}'.")

    def __delattr__(self, name: str) -> NoReturn:
        """Forbid attribute deletion on immutable `SecretValue`."""
        raise AttributeError(f"SecretValue is immutable; cannot delete '{name}'.")


# endregion


# region Validation
def scrub_secrets(
    message: str,
    redact: Iterable[str | SecretValue] = (),
) -> str:
    """Scrub explicit secret values and common credential patterns from `message`.

    Args:
        message: Candidate diagnostic or exception message string.
        redact: Explicit plaintext strings or `SecretValue` wrappers to mask.

    Returns:
        Sanitized message with secrets replaced by `REDACTED_PLACEHOLDER`.
    """
    raw_candidates = [item.expose() if isinstance(item, SecretValue) else item for item in redact]
    non_empty = sorted({val for val in raw_candidates if val}, key=len, reverse=True)

    scrubbed = message
    for secret_str in non_empty:
        scrubbed = scrubbed.replace(secret_str, REDACTED_PLACEHOLDER)

    scrubbed = CREDENTIAL_ASSIGNMENT_PATTERN.sub(rf"\1\2{REDACTED_PLACEHOLDER}", scrubbed)
    return BEARER_HEADER_PATTERN.sub(rf"\1\2{REDACTED_PLACEHOLDER}", scrubbed)


# endregion


# region Exceptions
class SecretError(Exception):
    """Base exception for `worktrack.secrets` that scrubs secret material."""

    def __init__(
        self,
        message: str,
        *,
        redact: Sequence[str | SecretValue] = (),
    ) -> None:
        """Initialize error with scrubbed diagnostic message."""
        sanitized = scrub_secrets(message, redact=redact)
        super().__init__(sanitized)


class SecretUriError(SecretError):
    """Raised when a `SecretUri` is malformed or uses an unsupported scheme."""


class SecretProviderUnavailableError(SecretError):
    """Raised when an optional secret backend (`keyring` or `boto3`) is absent."""


class SecretResolutionError(SecretError):
    """Raised when resolving a secret from its backing store fails."""


class SecretNotFoundError(SecretResolutionError):
    """Raised when a referenced secret key, file, or JSON attribute is missing."""


class SecretPermissionError(SecretResolutionError):
    """Raised when a secret or token file violates POSIX permission rules."""


class OAuthBrokerError(SecretError):
    """Raised when stateless OAuth PKCE token exchange or refresh fails."""


class OAuthInvalidClientError(OAuthBrokerError):
    """Raised when upstream OAuth rejects client credentials (`invalid_client`)."""


class UserTokenStoreError(SecretError):
    """Raised when workstation-local `UserTokenStore` persistence fails."""


# endregion
