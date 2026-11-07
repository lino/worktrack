"""`RetroSplitEngine` generating duration-conserving proposals and applying them to `Ledger`."""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from worktrack.agenda.planner import AgendaPlanner
from worktrack.core.exceptions import LedgerError
from worktrack.core.ledger import Ledger
from worktrack.core.models import CAPTURE_MODE_CHAOTIC, Activity, Interval
from worktrack.reconciler.exceptions import ReconcilerValidationError, RetroSplitConflictError
from worktrack.reconciler.models import (
    ALLOWED_RETROSPLIT_STRATEGIES,
    DEFAULT_EQUAL_WEIGHT,
    RETROSPLIT_STRATEGY_EQUAL,
    RETROSPLIT_STRATEGY_PROPORTIONAL,
    RETROSPLIT_STRATEGY_WEIGHTS,
    SINGLE_SLICE_COUNT,
    ZERO_SECONDS,
    ZERO_WEIGHT,
    PlausibilityGap,
    PlausibilityReport,
    ReconcileClueInput,
    RetroSplitProposal,
    RetroSplitResult,
    RetroSplitSliceProposal,
    coerce_clue_input,
    validate_positive_weight,
)
from worktrack.telemetry.metrics import TelemetryRecorder


# region RetroSplit Engine
class RetroSplitEngine:
    """Generates previewable `RetroSplitProposal`s and applies them atomically to `Ledger`."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        agenda_planner: AgendaPlanner | None = None,
        agenda: AgendaPlanner | None = None,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        """Initialize `RetroSplitEngine` bound to `ledger`, optional `agenda`, and `telemetry`."""
        self._ledger = ledger
        self._agenda = agenda_planner if agenda_planner is not None else agenda
        self._telemetry = telemetry

    def propose_split(
        self,
        interval: Interval | str | Sequence[Interval | str],
        *,
        strategy: str = RETROSPLIT_STRATEGY_PROPORTIONAL,
        activity_ids: Sequence[str] | None = None,
        gaps: Sequence[PlausibilityGap] | None = None,
        clues: Sequence[ReconcileClueInput | Mapping[str, object] | object] | None = None,
        weights: Mapping[str, float] | None = None,
        notes_by_activity: Mapping[str, str] | None = None,
    ) -> RetroSplitProposal:
        """Build a previewable `RetroSplitProposal` with exact second-level conservation."""
        if strategy not in ALLOWED_RETROSPLIT_STRATEGIES:
            allowed = ", ".join(sorted(ALLOWED_RETROSPLIT_STRATEGIES))
            raise ReconcilerValidationError(f"strategy must be one of: {allowed}.")

        source_intervals = self._resolve_source_intervals(interval)
        targets = self._resolve_targets(activity_ids, gaps, clues, weights)
        raw_weights = self._compute_raw_weights(strategy, targets, gaps, clues, weights)
        normalized = _normalize_weights(raw_weights)
        all_slices: list[RetroSplitSliceProposal] = []
        total_seconds = ZERO_SECONDS

        for src_interval in source_intervals:
            dur_sec = _require_closed_interval_seconds(src_interval)
            total_seconds += dur_sec
            all_slices.extend(
                _build_interval_slices(
                    src_interval,
                    dur_sec,
                    target_activities=targets,
                    raw_weights=raw_weights,
                    normalized_weights=normalized,
                    notes_by_activity=notes_by_activity,
                )
            )
        return RetroSplitProposal(
            proposal_id=str(uuid.uuid4()),
            strategy=strategy,
            source_intervals=tuple(source_intervals),
            slices=tuple(all_slices),
            total_duration_seconds=total_seconds,
            normalized_weights=normalized,
            created_at=datetime.now(UTC),
        )

    propose = propose_split

    def propose_from_report(
        self,
        report: PlausibilityReport,
        *,
        strategy: str = RETROSPLIT_STRATEGY_PROPORTIONAL,
        weights: Mapping[str, float] | None = None,
        notes_by_activity: Mapping[str, str] | None = None,
    ) -> RetroSplitProposal:
        """Build a `RetroSplitProposal` directly from a `PlausibilityReport`."""
        if not report.candidate_intervals:
            raise ReconcilerValidationError("PlausibilityReport has no candidate Intervals.")
        if not report.gaps and not weights:
            raise ReconcilerValidationError("PlausibilityReport has no gaps to allocate.")
        return self.propose_split(
            report.candidate_intervals,
            strategy=strategy,
            gaps=report.gaps,
            weights=weights,
            notes_by_activity=notes_by_activity,
        )

    def apply_proposal(
        self,
        proposal: RetroSplitProposal,
        *,
        allow_rebind: bool = True,
    ) -> RetroSplitResult:
        """Atomically bind (1 slice) or split (>= 2 slices) candidate `Intervals` in `Ledger`."""
        if not proposal.source_intervals or not proposal.slices or not proposal.is_conserved:
            raise ReconcilerValidationError("Invalid or non-conserved RetroSplitProposal.")

        applied_intervals: list[Interval] = []
        for src in proposal.source_intervals:
            interval_slices = proposal.slices_for_interval(src.id)
            if not interval_slices:
                raise ReconcilerValidationError(f"No slices found for Interval '{src.id}'.")
            applied_intervals.extend(
                self._apply_single_interval(src, interval_slices, allow_rebind=allow_rebind)
            )
        return RetroSplitResult(
            proposal_id=proposal.proposal_id,
            strategy=proposal.strategy,
            intervals=tuple(applied_intervals),
            total_duration_seconds=proposal.total_duration_seconds,
            applied_at=datetime.now(UTC),
        )

    def _apply_single_interval(
        self,
        source_interval: Interval,
        slices: Sequence[RetroSplitSliceProposal],
        *,
        allow_rebind: bool,
    ) -> list[Interval]:
        """Verify state and bind or split a single candidate `Interval` in `Ledger`."""
        try:
            current = self._ledger.get_interval(source_interval.id)
        except LedgerError as exc:
            raise RetroSplitConflictError(
                f"Candidate Interval '{source_interval.id}' is no longer present."
            ) from exc
        current_sec = _require_closed_interval_seconds(current, conflict_on_open=True)
        expected_sec = sum(item.duration_seconds for item in slices)
        if current_sec != expected_sec:
            raise RetroSplitConflictError(
                f"Interval '{current.id}' duration changed ({current_sec}s != {expected_sec}s)."
            )

        archived_ids = self._temporarily_unarchive(slices)
        try:
            if len(slices) == SINGLE_SLICE_COUNT:
                only = slices[0]
                bound = self._ledger.bind_interval(
                    current.id,
                    only.activity_id,
                    notes=only.notes,
                    tags=only.tags if only.tags else None,
                    allow_rebind=allow_rebind,
                )
                self._record_chaotic_telemetry(SINGLE_SLICE_COUNT)
                return [bound]

            children = self._ledger.split_interval(
                current.id, [item.to_interval_slice() for item in slices]
            )
            self._record_chaotic_telemetry(len(children))
            return children
        finally:
            for act_id in archived_ids:
                self._ledger.update_activity(act_id, archived=True)

    def _temporarily_unarchive(self, slices: Sequence[RetroSplitSliceProposal]) -> list[str]:
        """Temporarily unarchive any archived target `Activities` during retroactive split."""
        unarchived: list[str] = []
        for act_id in dict.fromkeys(item.activity_id for item in slices):
            act = self._ledger.get_activity(act_id)
            if act.archived:
                self._ledger.update_activity(act.id, archived=False)
                unarchived.append(act.id)
        return unarchived

    def _record_chaotic_telemetry(self, count: int) -> None:
        """Increment `worktrack_intervals_recorded_total{mode="chaotic"}` via recorder."""
        recorder: TelemetryRecorder | None = self._telemetry or getattr(
            self._ledger, "_telemetry", None
        )
        if recorder is not None:
            for _ in range(count):
                recorder.record_interval(mode=CAPTURE_MODE_CHAOTIC)

    def _resolve_source_intervals(
        self, raw: Interval | str | Sequence[Interval | str]
    ) -> list[Interval]:
        """Resolve one or more `Interval` instances or IDs from `Ledger`."""
        items: Sequence[Interval | str] = [raw] if isinstance(raw, (Interval, str)) else list(raw)
        if not items:
            raise ReconcilerValidationError("At least one candidate Interval is required.")
        return [
            self._ledger.get_interval(item) if isinstance(item, str) else item for item in items
        ]

    def _resolve_targets(
        self,
        activity_ids: Sequence[str] | None,
        gaps: Sequence[PlausibilityGap] | None,
        clues: Sequence[ReconcileClueInput | Mapping[str, object] | object] | None,
        weights: Mapping[str, float] | None,
    ) -> list[Activity]:
        """Determine ordered unique target `Activities` for a split proposal."""
        raw_ids = _extract_raw_candidate_ids(activity_ids, gaps, clues, weights)
        ordered_ids = list(dict.fromkeys(item.strip() for item in raw_ids if item.strip()))
        if not ordered_ids:
            raise ReconcilerValidationError("At least one target Activity is required.")
        try:
            return [self._ledger.get_activity(act_id) for act_id in ordered_ids]
        except LedgerError as exc:
            raise ReconcilerValidationError(str(exc)) from exc

    def _compute_raw_weights(
        self,
        strategy: str,
        targets: Sequence[Activity],
        gaps: Sequence[PlausibilityGap] | None,
        clues: Sequence[ReconcileClueInput | Mapping[str, object] | object] | None,
        weights: Mapping[str, float] | None,
    ) -> dict[str, float]:
        """Compute positive raw weights for each target `Activity` by strategy."""
        equal_map = dict.fromkeys((act.id for act in targets), DEFAULT_EQUAL_WEIGHT)
        if strategy == RETROSPLIT_STRATEGY_EQUAL:
            return equal_map
        if strategy == RETROSPLIT_STRATEGY_WEIGHTS or weights:
            return _extract_explicit_weights(targets, weights)

        gap_map = {gap.activity.id: gap for gap in (gaps or ())}
        clue_weights = _tally_clue_weights(targets, gap_map, clues)
        if max(clue_weights.values(), default=ZERO_WEIGHT) > ZERO_WEIGHT:
            return _fill_zero_weights(clue_weights, DEFAULT_EQUAL_WEIGHT)

        budget_weights = self._tally_budget_weights(targets, gap_map)
        if max(budget_weights.values(), default=ZERO_WEIGHT) > ZERO_WEIGHT:
            return _fill_zero_weights(budget_weights)
        return equal_map

    def _tally_budget_weights(
        self,
        targets: Sequence[Activity],
        gap_map: Mapping[str, PlausibilityGap],
    ) -> dict[str, float]:
        """Extract `AgendaItem` budget seconds per target `Activity`."""
        budgets: dict[str, float] = {
            act.id: float(gap_map[act.id].agenda_budget_seconds or ZERO_SECONDS)
            if act.id in gap_map
            else ZERO_WEIGHT
            for act in targets
        }
        if self._agenda is not None:
            for item in self._agenda.list_items(include_done=True):
                if item.activity_id in budgets and item.budget_seconds:
                    budgets[item.activity_id] = max(
                        budgets[item.activity_id], float(item.budget_seconds)
                    )
        return budgets


def _fill_zero_weights(
    weights: Mapping[str, float],
    fallback: float | None = None,
) -> dict[str, float]:
    """Replace any non-positive weight entries with `fallback` or average positive weight."""
    pos = [val for val in weights.values() if val > ZERO_WEIGHT]
    resolved_fallback = fallback if fallback is not None else (sum(pos) / len(pos))
    return {
        act_id: (val if val > ZERO_WEIGHT else resolved_fallback)
        for act_id, val in weights.items()
    }


def _extract_raw_candidate_ids(
    activity_ids: Sequence[str] | None,
    gaps: Sequence[PlausibilityGap] | None,
    clues: Sequence[ReconcileClueInput | Mapping[str, object] | object] | None,
    weights: Mapping[str, float] | None,
) -> list[str]:
    """Extract raw candidate `Activity` IDs from the highest-priority input source."""
    if activity_ids:
        return list(activity_ids)
    if weights:
        return list(weights.keys())
    if gaps:
        return [gap.activity.id for gap in gaps]
    if not clues:
        return []
    return [
        coerced.activity_id
        for raw in clues
        if (coerced := coerce_clue_input(raw)).activity_id is not None
    ]


def _extract_explicit_weights(
    targets: Sequence[Activity],
    weights: Mapping[str, float] | None,
) -> dict[str, float]:
    """Validate explicit `weights` mapping for all `targets`."""
    if not weights:
        raise ReconcilerValidationError("Explicit weights mapping is required.")
    resolved: dict[str, float] = {}
    for act in targets:
        if act.id not in weights:
            raise ReconcilerValidationError(f"Missing weight for Activity '{act.id}'.")
        resolved[act.id] = validate_positive_weight(
            weights[act.id], field_name=f"weights[{act.id}]"
        )
    return resolved


def _tally_clue_weights(
    targets: Sequence[Activity],
    gap_map: Mapping[str, PlausibilityGap],
    clues: Sequence[ReconcileClueInput | Mapping[str, object] | object] | None,
) -> dict[str, float]:
    """Sum clue weights per target `Activity` from explicit `clues` or `gaps`."""
    if clues is None:
        return {
            act.id: (gap_map[act.id].clue_weight_total if act.id in gap_map else ZERO_WEIGHT)
            for act in targets
        }
    tallies: dict[str, float] = {act.id: ZERO_WEIGHT for act in targets}
    ref_to_id = {act.external_ref: act.id for act in targets if act.external_ref}
    for raw in clues:
        coerced = coerce_clue_input(raw)
        act_id = coerced.activity_id or ref_to_id.get(coerced.external_ref or "")
        if act_id in tallies:
            tallies[act_id] += coerced.weight
    return tallies


def _normalize_weights(raw_weights: Mapping[str, float]) -> dict[str, float]:
    """Normalize positive raw weights so their shares sum to `1.0`."""
    total = sum(raw_weights.values())
    if total <= ZERO_WEIGHT:
        raise ReconcilerValidationError("Total weight across target Activities must be > 0.")
    return {act_id: weight / total for act_id, weight in raw_weights.items()}


def _require_closed_interval_seconds(interval: Interval, *, conflict_on_open: bool = False) -> int:
    """Validate that `interval` is closed and return its whole duration in seconds."""
    if interval.ended_at is None or interval.duration_seconds is None:
        if conflict_on_open:
            raise RetroSplitConflictError(f"Interval '{interval.id}' is still open.")
        raise ReconcilerValidationError(f"Cannot split open Interval '{interval.id}'.")
    seconds = round(interval.duration_seconds)
    if seconds <= ZERO_SECONDS:
        raise ReconcilerValidationError(f"Interval '{interval.id}' has non-positive duration.")
    return seconds


def _allocate_conserved_seconds(
    total_seconds: int,
    ordered_ids: Sequence[str],
    raw_weights: Mapping[str, float],
    normalized_weights: Mapping[str, float],
) -> list[int]:
    """Allocate integer seconds summing to `total_seconds`, remainder to highest weight."""
    count = len(ordered_ids)
    if total_seconds < count:
        raise ReconcilerValidationError(
            f"Interval duration ({total_seconds}s) is shorter than target slice count ({count})."
        )
    durations = [
        max(SINGLE_SLICE_COUNT, math.floor(total_seconds * normalized_weights[act_id]))
        for act_id in ordered_ids
    ]
    remainder = total_seconds - sum(durations)
    winner_idx = max(range(count), key=lambda idx: (raw_weights[ordered_ids[idx]], -idx))
    durations[winner_idx] += remainder
    return durations


def _build_interval_slices(
    source_interval: Interval,
    total_seconds: int,
    *,
    target_activities: Sequence[Activity],
    raw_weights: Mapping[str, float],
    normalized_weights: Mapping[str, float],
    notes_by_activity: Mapping[str, str] | None,
) -> list[RetroSplitSliceProposal]:
    """Build contiguous `RetroSplitSliceProposal` objects for `source_interval`."""
    assert source_interval.ended_at is not None
    ordered_ids = [act.id for act in target_activities]
    durations = _allocate_conserved_seconds(
        total_seconds, ordered_ids, raw_weights, normalized_weights
    )
    cursor = source_interval.started_at
    slices: list[RetroSplitSliceProposal] = []
    last_idx = len(target_activities) - 1

    for idx, (act, dur_sec) in enumerate(zip(target_activities, durations, strict=True)):
        slice_end = (
            source_interval.ended_at if idx == last_idx else cursor + timedelta(seconds=dur_sec)
        )
        custom_note = None if notes_by_activity is None else notes_by_activity.get(act.id)
        slices.append(
            RetroSplitSliceProposal(
                interval_id=source_interval.id,
                activity_id=act.id,
                activity_name=act.name,
                started_at=cursor,
                ended_at=slice_end,
                duration_seconds=dur_sec,
                weight=raw_weights[act.id],
                share=normalized_weights[act.id],
                notes=source_interval.notes if custom_note is None else custom_note,
                tags=source_interval.tags,
            )
        )
        cursor = slice_end
    return slices


# endregion
