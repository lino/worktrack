"""Plausibility `Reconciler` and proportional/equal/weighted `RetroSplit` engine."""

from __future__ import annotations

from worktrack.reconciler.detector import PlausibilityReconciler
from worktrack.reconciler.engine import RetroSplitEngine
from worktrack.reconciler.exceptions import (
    ReconcilerError,
    ReconcilerValidationError,
    RetroSplitConflictError,
)
from worktrack.reconciler.models import (
    ALLOWED_GAP_REASONS,
    ALLOWED_RETROSPLIT_STRATEGIES,
    COMPLETED_ACTIVITY_TAGS,
    DEFAULT_CLUE_SOURCE,
    DEFAULT_CLUE_WEIGHT,
    DEFAULT_EQUAL_WEIGHT,
    DEFAULT_MIN_BOOKED_SECONDS,
    GAP_REASON_BELOW_MIN_DURATION,
    GAP_REASON_CLUE_WITHOUT_INTERVAL,
    GAP_REASON_COMPLETED_ACTIVITY,
    RETROSPLIT_STRATEGY_EQUAL,
    RETROSPLIT_STRATEGY_PROPORTIONAL,
    RETROSPLIT_STRATEGY_WEIGHTS,
    PlausibilityGap,
    PlausibilityReport,
    ReconcileClueInput,
    RetroSplitProposal,
    RetroSplitResult,
    RetroSplitSliceProposal,
    coerce_clue_input,
    validate_positive_weight,
)

__all__ = [
    "ALLOWED_GAP_REASONS",
    "ALLOWED_RETROSPLIT_STRATEGIES",
    "COMPLETED_ACTIVITY_TAGS",
    "DEFAULT_CLUE_SOURCE",
    "DEFAULT_CLUE_WEIGHT",
    "DEFAULT_EQUAL_WEIGHT",
    "DEFAULT_MIN_BOOKED_SECONDS",
    "GAP_REASON_BELOW_MIN_DURATION",
    "GAP_REASON_CLUE_WITHOUT_INTERVAL",
    "GAP_REASON_COMPLETED_ACTIVITY",
    "RETROSPLIT_STRATEGY_EQUAL",
    "RETROSPLIT_STRATEGY_PROPORTIONAL",
    "RETROSPLIT_STRATEGY_WEIGHTS",
    "PlausibilityGap",
    "PlausibilityReconciler",
    "PlausibilityReport",
    "ReconcileClueInput",
    "ReconcilerError",
    "ReconcilerValidationError",
    "RetroSplitConflictError",
    "RetroSplitEngine",
    "RetroSplitProposal",
    "RetroSplitResult",
    "RetroSplitSliceProposal",
    "coerce_clue_input",
    "validate_positive_weight",
]
