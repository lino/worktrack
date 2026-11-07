"""Domain exception hierarchy for the Worktrack SQLite ledger (`worktrack.core`)."""

from __future__ import annotations


class LedgerError(Exception):
    """Base exception for all Worktrack domain ledger errors."""


class MigrationError(LedgerError):
    """Raised when SQLite schema migrations fail or checksums do not match."""


class LedgerValidationError(LedgerError, ValueError):
    """Raised when domain inputs violate invariants, bounds, or schemas."""


class NotFoundError(LedgerError, LookupError):
    """Raised when a requested Ledger domain record does not exist."""


class ActivityNotFoundError(NotFoundError):
    """Raised when an `Activity` identifier cannot be found in the Ledger."""


class IntervalNotFoundError(NotFoundError):
    """Raised when an `Interval` identifier cannot be found in the Ledger."""


class EntityNotFoundError(NotFoundError):
    """Raised when an `Entity` identifier cannot be found in the Ledger."""


class FacetNotFoundError(NotFoundError):
    """Raised when a `Facet` schema key cannot be found in the Ledger."""


class DuplicateKeyError(LedgerValidationError):
    """Raised when registering a duplicate unique `Entity` or `Facet` key."""


class IntervalStateError(LedgerValidationError):
    """Raised when an `Interval` state transition is invalid."""


class NoActiveIntervalError(IntervalStateError):
    """Raised when `clock_out` is invoked with zero active open `Intervals`."""


class OverlapConflictError(IntervalStateError):
    """Raised when open `Intervals` conflict with overlap or clock-out rules."""


class AttributionCycleError(LedgerValidationError):
    """Raised when an `Entity -> Entity` `Attribution` edge introduces a cycle."""


class InvalidAttributionWeightError(LedgerValidationError):
    """Raised when an `Attribution` weight violates `(0.0, 1.0]` or sum bounds."""


class FacetValueValidationError(LedgerValidationError):
    """Raised when a `ContextFact` value violates its `Facet` type or allowlist."""
