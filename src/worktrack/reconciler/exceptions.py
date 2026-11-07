"""Domain exception hierarchy for `worktrack.reconciler`."""

from __future__ import annotations

from worktrack.core.exceptions import IntervalStateError, LedgerError, LedgerValidationError


class ReconcilerError(LedgerError):
    """Base exception for plausibility reconciliation and `RetroSplit` operations."""


class ReconcilerValidationError(ReconcilerError, LedgerValidationError):
    """Raised when reconciler window bounds, clues, strategies, or weights are invalid."""


class RetroSplitConflictError(ReconcilerError, IntervalStateError):
    """Raised when a `RetroSplitProposal` conflicts with current `Ledger` interval state."""


__all__ = [
    "ReconcilerError",
    "ReconcilerValidationError",
    "RetroSplitConflictError",
]
