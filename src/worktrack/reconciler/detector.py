"""`PlausibilityReconciler` inspecting time windows for unbooked work and chaotic blocks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from worktrack.agenda.models import AGENDA_STATUS_DONE
from worktrack.agenda.planner import AgendaPlanner
from worktrack.core.ledger import Ledger
from worktrack.core.models import (
    CAPTURE_MODE_CHAOTIC,
    Activity,
    Interval,
    ensure_utc,
)
from worktrack.reconciler.exceptions import ReconcilerValidationError
from worktrack.reconciler.models import (
    COMPLETED_ACTIVITY_TAGS,
    DEFAULT_MIN_BOOKED_SECONDS,
    GAP_REASON_BELOW_MIN_DURATION,
    GAP_REASON_CLUE_WITHOUT_INTERVAL,
    GAP_REASON_COMPLETED_ACTIVITY,
    ZERO_SECONDS,
    PlausibilityGap,
    PlausibilityReport,
    ReconcileClueInput,
    coerce_clue_input,
)


# region Plausibility Detector
class PlausibilityReconciler:
    """Inspects `[start, end)` windows for plausibility gaps and chaotic `Intervals`."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        agenda_planner: AgendaPlanner | None = None,
        agenda: AgendaPlanner | None = None,
        min_booked_seconds: int = DEFAULT_MIN_BOOKED_SECONDS,
    ) -> None:
        """Initialize reconciler bound to `ledger` and optional `agenda_planner`."""
        self._ledger = ledger
        self._agenda = agenda_planner if agenda_planner is not None else agenda
        self._min_booked_seconds = _validate_min_booked(min_booked_seconds)

    @property
    def min_booked_seconds(self) -> int:
        """Return configured minimum booked duration threshold in seconds."""
        return self._min_booked_seconds

    def inspect(
        self,
        start: datetime,
        end: datetime,
        *,
        clues: Sequence[ReconcileClueInput | Mapping[str, object] | object] = (),
        completed_activity_ids: Sequence[str] = (),
        min_booked_seconds: int | None = None,
        include_chaotic_allocated: bool = False,
    ) -> PlausibilityReport:
        """Inspect `[start, end)` and return a `PlausibilityReport`."""
        utc_start, utc_end = _validate_window(start, end)
        threshold = (
            self._min_booked_seconds
            if min_booked_seconds is None
            else _validate_min_booked(min_booked_seconds)
        )
        activities_by_id, activities_by_ref = _index_activities(self._ledger)
        booked_map, candidates, unalloc_sec, chaotic_sec = _scan_window_intervals(
            self._ledger,
            utc_start,
            utc_end,
            include_chaotic_allocated=include_chaotic_allocated,
        )
        done_in_agenda, agenda_budgets = _collect_agenda_signals(self._agenda)
        explicit_done = _collect_completed_ids(activities_by_id, completed_activity_ids)
        clues_by_activity = _group_window_clues(
            clues,
            utc_start,
            utc_end,
            activities_by_id=activities_by_id,
            activities_by_ref=activities_by_ref,
        )
        gaps = _build_plausibility_gaps(
            activities_by_id=activities_by_id,
            booked_map=booked_map,
            threshold=threshold,
            done_in_agenda=done_in_agenda,
            explicit_done=explicit_done,
            agenda_budgets=agenda_budgets,
            clues_by_activity=clues_by_activity,
        )
        return PlausibilityReport(
            window_start=utc_start,
            window_end=utc_end,
            min_booked_seconds=threshold,
            gaps=tuple(gaps),
            candidate_intervals=tuple(candidates),
            total_unallocated_seconds=unalloc_sec,
            total_chaotic_seconds=chaotic_sec,
        )

    def detect_gaps(
        self,
        start: datetime,
        end: datetime,
        *,
        clues: Sequence[ReconcileClueInput | Mapping[str, object] | object] = (),
        completed_activity_ids: Sequence[str] = (),
        min_booked_seconds: int | None = None,
    ) -> list[PlausibilityGap]:
        """Return detected `PlausibilityGaps` within `[start, end)`."""
        report = self.inspect(
            start,
            end,
            clues=clues,
            completed_activity_ids=completed_activity_ids,
            min_booked_seconds=min_booked_seconds,
        )
        return list(report.gaps)

    def find_candidate_intervals(
        self,
        start: datetime,
        end: datetime,
        *,
        include_chaotic_allocated: bool = False,
    ) -> list[Interval]:
        """Return candidate unallocated/chaotic `Intervals` overlapping `[start, end)`."""
        report = self.inspect(
            start,
            end,
            include_chaotic_allocated=include_chaotic_allocated,
        )
        return list(report.candidate_intervals)


def _validate_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Validate and normalize `[start, end)` to UTC."""
    try:
        utc_start = ensure_utc(start, field_name="start")
        utc_end = ensure_utc(end, field_name="end")
    except Exception as exc:
        raise ReconcilerValidationError(str(exc)) from exc
    if utc_end <= utc_start:
        raise ReconcilerValidationError("end must be strictly greater than start.")
    return utc_start, utc_end


def _validate_min_booked(min_booked_seconds: int) -> int:
    """Validate non-negative integer `min_booked_seconds`."""
    if isinstance(min_booked_seconds, bool) or min_booked_seconds < ZERO_SECONDS:
        raise ReconcilerValidationError("min_booked_seconds must be an integer >= 0.")
    return int(min_booked_seconds)


def _index_activities(ledger: Ledger) -> tuple[dict[str, Activity], dict[str, Activity]]:
    """Index all `Activities` by `id` and `external_ref`."""
    by_id: dict[str, Activity] = {}
    by_ref: dict[str, Activity] = {}
    for activity in ledger.list_activities(include_archived=True):
        by_id[activity.id] = activity
        if activity.external_ref is not None and activity.external_ref not in by_ref:
            by_ref[activity.external_ref] = activity
    return by_id, by_ref


def _scan_window_intervals(
    ledger: Ledger,
    utc_start: datetime,
    utc_end: datetime,
    *,
    include_chaotic_allocated: bool,
) -> tuple[dict[str, int], list[Interval], int, int]:
    """Compute per-activity booked seconds and collect candidate chaotic `Intervals`."""
    intervals = ledger.list_intervals(since=utc_start, until=utc_end, include_open=False)
    booked_by_activity: dict[str, int] = {}
    candidates: list[Interval] = []
    unallocated_seconds = ZERO_SECONDS
    chaotic_seconds = ZERO_SECONDS

    for interval in intervals:
        if interval.ended_at is None:
            continue
        overlap_start = max(interval.started_at, utc_start)
        overlap_end = min(interval.ended_at, utc_end)
        overlap_sec = max(ZERO_SECONDS, round((overlap_end - overlap_start).total_seconds()))
        full_sec = max(
            ZERO_SECONDS, round((interval.ended_at - interval.started_at).total_seconds())
        )

        if interval.activity_id is not None:
            booked_by_activity[interval.activity_id] = (
                booked_by_activity.get(interval.activity_id, ZERO_SECONDS) + overlap_sec
            )
        if interval.activity_id is None:
            unallocated_seconds += full_sec
            chaotic_seconds += full_sec
            candidates.append(interval)
        elif interval.capture_mode == CAPTURE_MODE_CHAOTIC:
            chaotic_seconds += full_sec
            if include_chaotic_allocated:
                candidates.append(interval)

    return booked_by_activity, candidates, unallocated_seconds, chaotic_seconds


def _collect_agenda_signals(
    agenda: AgendaPlanner | None,
) -> tuple[set[str], dict[str, int]]:
    """Extract completed `Activity` IDs and planned budgets from `AgendaPlanner`."""
    done_ids: set[str] = set()
    budgets: dict[str, int] = {}
    if agenda is None:
        return done_ids, budgets
    for item in agenda.list_items(include_done=True):
        if item.budget_seconds is not None and item.budget_seconds > ZERO_SECONDS:
            budgets[item.activity_id] = max(
                budgets.get(item.activity_id, ZERO_SECONDS),
                item.budget_seconds,
            )
        if item.status == AGENDA_STATUS_DONE:
            done_ids.add(item.activity_id)
    return done_ids, budgets


def _collect_completed_ids(
    activities_by_id: Mapping[str, Activity],
    completed_activity_ids: Sequence[str],
) -> set[str]:
    """Combine explicit `completed_activity_ids` with archived or `done`-tagged `Activities`."""
    completed: set[str] = {item.strip() for item in completed_activity_ids if item.strip()}
    for activity in activities_by_id.values():
        if activity.archived or bool(COMPLETED_ACTIVITY_TAGS & set(activity.tags)):
            completed.add(activity.id)
    return completed


def _group_window_clues(
    raw_clues: Sequence[ReconcileClueInput | Mapping[str, object] | object],
    utc_start: datetime,
    utc_end: datetime,
    *,
    activities_by_id: Mapping[str, Activity],
    activities_by_ref: Mapping[str, Activity],
) -> dict[str, list[ReconcileClueInput]]:
    """Filter clues to `[utc_start, utc_end)` and group by resolved `Activity.id`."""
    grouped: dict[str, list[ReconcileClueInput]] = {}
    for raw in raw_clues:
        clue = coerce_clue_input(raw)
        if clue.timestamp is not None and not (utc_start <= clue.timestamp < utc_end):
            continue
        resolved_id = _resolve_clue_activity_id(
            clue,
            activities_by_id=activities_by_id,
            activities_by_ref=activities_by_ref,
        )
        if resolved_id is not None:
            grouped.setdefault(resolved_id, []).append(clue)
    return grouped


def _resolve_clue_activity_id(
    clue: ReconcileClueInput,
    *,
    activities_by_id: Mapping[str, Activity],
    activities_by_ref: Mapping[str, Activity],
) -> str | None:
    """Resolve `clue.activity_id` or `clue.external_ref` to a known `Activity.id`."""
    if clue.activity_id is not None and clue.activity_id in activities_by_id:
        return clue.activity_id
    if clue.external_ref is not None:
        if clue.external_ref in activities_by_ref:
            return activities_by_ref[clue.external_ref].id
        if clue.external_ref in activities_by_id:
            return clue.external_ref
    return None


def _build_plausibility_gaps(
    *,
    activities_by_id: Mapping[str, Activity],
    booked_map: Mapping[str, int],
    threshold: int,
    done_in_agenda: set[str],
    explicit_done: set[str],
    agenda_budgets: Mapping[str, int],
    clues_by_activity: Mapping[str, list[ReconcileClueInput]],
) -> list[PlausibilityGap]:
    """Construct ordered `PlausibilityGap` entries for under-booked `Activities`."""
    candidate_ids = sorted(
        (done_in_agenda | explicit_done | set(clues_by_activity.keys()))
        & set(activities_by_id.keys())
    )
    gaps: list[PlausibilityGap] = []
    for act_id in candidate_ids:
        booked = booked_map.get(act_id, ZERO_SECONDS)
        if booked > ZERO_SECONDS and booked >= threshold:
            continue
        activity = activities_by_id[act_id]
        clue_list = tuple(clues_by_activity.get(act_id, ()))
        is_completed = act_id in done_in_agenda or act_id in explicit_done
        reasons = _derive_gap_reasons(
            is_completed=is_completed,
            has_clues=bool(clue_list),
            booked_seconds=booked,
            threshold=threshold,
        )
        budget_sec = agenda_budgets.get(act_id)
        target_sec = budget_sec if budget_sec is not None else threshold
        gaps.append(
            PlausibilityGap(
                activity=activity,
                booked_seconds=booked,
                min_booked_seconds=threshold,
                deficit_seconds=max(ZERO_SECONDS, target_sec - booked),
                clue_count=len(clue_list),
                clue_weight_total=sum(item.weight for item in clue_list),
                agenda_budget_seconds=budget_sec,
                completed_in_agenda=act_id in done_in_agenda,
                reasons=reasons,
                clues=clue_list,
            )
        )
    return gaps


def _derive_gap_reasons(
    *,
    is_completed: bool,
    has_clues: bool,
    booked_seconds: int,
    threshold: int,
) -> tuple[str, ...]:
    """Build ordered tuple of plausibility gap reason codes."""
    reasons: list[str] = []
    if is_completed:
        reasons.append(GAP_REASON_COMPLETED_ACTIVITY)
    if has_clues:
        reasons.append(GAP_REASON_CLUE_WITHOUT_INTERVAL)
    if booked_seconds > ZERO_SECONDS and booked_seconds < threshold:
        reasons.append(GAP_REASON_BELOW_MIN_DURATION)
    if not reasons:
        reasons.append(GAP_REASON_BELOW_MIN_DURATION)
    return tuple(reasons)


# endregion
