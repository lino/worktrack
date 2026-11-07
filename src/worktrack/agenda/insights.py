"""Horizon insight calculators (`Now`, `Today`, `This Week`) and IPC snapshot mapper."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from worktrack.agenda.models import (
    DAYS_PER_WEEK,
    DEFAULT_APPROACHING_DEADLINE_HOURS,
    HORIZON_THIS_WEEK,
    HORIZON_TODAY,
    OPEN_AGENDA_STATUSES,
    UNALLOCATED_ACTIVITY_KEY,
    UNALLOCATED_ACTIVITY_NAME,
    ZERO_SECONDS_INT,
    ActivityDurationSummary,
    AgendaItem,
    AgendaQueueEntry,
    FocusBlock,
    HorizonInsight,
    NowInsight,
)
from worktrack.agenda.persistence import list_agenda_items, list_focus_blocks
from worktrack.agenda.validators import activity_display_key
from worktrack.core.ledger import Ledger
from worktrack.core.models import (
    BUILTIN_WORKPLACE_FACET_KEY,
    BUILTIN_WORKPLACE_VALUES,
    Activity,
    Interval,
    ensure_utc,
    format_utc_iso,
)
from worktrack.ipc.models import AgendaItemSummary, AgendaTodaySnapshot, FocusBlockSummary

# region Constants
SINGLE_DAY: Final = 1
ZERO_HOUR: Final = 0
# endregion


# region Insights
def compute_now_insight(
    conn: sqlite3.Connection,
    ledger: Ledger,
    *,
    now: datetime | None = None,
    approaching_deadline_hours: int = DEFAULT_APPROACHING_DEADLINE_HOURS,
) -> NowInsight:
    """Compute real-time `NowInsight` answering 'what am I doing right now?'."""
    utc_now = _resolve_now(now)
    day_start, day_end = _today_bounds(utc_now)
    active_interval, active_activity, elapsed = _resolve_active_timer(ledger, utc_now)
    active_block, upcoming_block = _split_focus_blocks(
        list_focus_blocks(conn, since=day_start, until=day_end), utc_now
    )
    intervals = ledger.list_intervals(since=day_start, until=day_end, include_open=True)
    act_map = {act.id: act for act in ledger.list_activities(include_archived=True)}
    totals_by_act, _, total_seconds = _accumulate_window_seconds(
        intervals, day_start, day_end, utc_now
    )
    queue = _load_open_queue(
        conn, (HORIZON_TODAY,), act_map, totals_by_act, utc_now, approaching_deadline_hours
    )
    return NowInsight(
        reference_time=utc_now,
        date_iso=day_start.date().isoformat(),
        active_interval=active_interval,
        active_activity=active_activity,
        active_elapsed_seconds=elapsed,
        active_focus_block=active_block,
        upcoming_focus_block=upcoming_block,
        next_agenda_item=queue[0] if queue else None,
        today_total_seconds=total_seconds,
        workplace=_resolve_current_workplace(ledger, active_interval, day_start),
    )


def compute_today_insight(
    conn: sqlite3.Connection,
    ledger: Ledger,
    *,
    now: datetime | None = None,
    approaching_deadline_hours: int = DEFAULT_APPROACHING_DEADLINE_HOURS,
) -> HorizonInsight:
    """Compute `HorizonInsight` for the current UTC day (`today`)."""
    utc_now = _resolve_now(now)
    day_start, day_end = _today_bounds(utc_now)
    return _compute_window_insight(
        conn,
        ledger,
        HORIZON_TODAY,
        (HORIZON_TODAY,),
        day_start,
        day_end,
        utc_now,
        approaching_deadline_hours,
    )


def compute_week_insight(
    conn: sqlite3.Connection,
    ledger: Ledger,
    *,
    now: datetime | None = None,
    approaching_deadline_hours: int = DEFAULT_APPROACHING_DEADLINE_HOURS,
) -> HorizonInsight:
    """Compute `HorizonInsight` for the current ISO Monday..Sunday UTC week."""
    utc_now = _resolve_now(now)
    day_start, _ = _today_bounds(utc_now)
    week_start = day_start - timedelta(days=day_start.weekday())
    return _compute_window_insight(
        conn,
        ledger,
        HORIZON_THIS_WEEK,
        (HORIZON_TODAY, HORIZON_THIS_WEEK),
        week_start,
        week_start + timedelta(days=DAYS_PER_WEEK),
        utc_now,
        approaching_deadline_hours,
    )


def build_ipc_today_snapshot(
    insight: HorizonInsight,
    activities_by_id: Mapping[str, Activity] | None = None,
) -> AgendaTodaySnapshot:
    """Convert a `today` `HorizonInsight` into a wire-ready `AgendaTodaySnapshot`."""
    act_lookup = dict(activities_by_id or {})
    for entry in insight.agenda_queue:
        act_lookup[entry.activity.id] = entry.activity

    items = tuple(
        AgendaItemSummary(
            activity_key=activity_display_key(entry.activity),
            title=entry.activity.name,
            budget_seconds=entry.item.budget_seconds or ZERO_SECONDS_INT,
            logged_seconds=entry.tracked_seconds,
            deadline=entry.item.deadline,
        )
        for entry in insight.agenda_queue
    )
    focus_blocks = tuple(
        FocusBlockSummary(
            block_id=block.id,
            activity_key=_focus_block_key(block, act_lookup),
            start_at=format_utc_iso(block.started_at),
            end_at=format_utc_iso(block.ended_at),
        )
        for block in insight.focus_blocks
    )
    return AgendaTodaySnapshot(
        date_iso=insight.date_iso,
        items=items,
        focus_blocks=focus_blocks,
        planned_budget_seconds=insight.planned_budget_seconds,
        logged_total_seconds=insight.total_tracked_seconds,
        workplace=insight.workplace,
    )


# endregion


# region Resolution
def _resolve_now(now: datetime | None) -> datetime:
    """Return validated UTC `now` timestamp defaulting to wall clock."""
    return ensure_utc(datetime.now(UTC) if now is None else now, field_name="now")


def _today_bounds(utc_now: datetime) -> tuple[datetime, datetime]:
    """Return `[00:00:00Z, next 00:00:00Z)` bounds for `utc_now`."""
    day_start = utc_now.replace(
        hour=ZERO_HOUR, minute=ZERO_HOUR, second=ZERO_HOUR, microsecond=ZERO_HOUR
    )
    return day_start, day_start + timedelta(days=SINGLE_DAY)


def _resolve_active_timer(
    ledger: Ledger,
    utc_now: datetime,
) -> tuple[Interval | None, Activity | None, int]:
    """Locate active open `Interval`, its bound `Activity`, and elapsed seconds."""
    open_intervals = ledger.get_open_intervals()
    if not open_intervals:
        return None, None, ZERO_SECONDS_INT
    active = open_intervals[-1]
    activity = None if active.activity_id is None else ledger.get_activity(active.activity_id)
    elapsed = max(ZERO_SECONDS_INT, round((utc_now - active.started_at).total_seconds()))
    return active, activity, elapsed


def _split_focus_blocks(
    blocks: Sequence[FocusBlock],
    utc_now: datetime,
) -> tuple[FocusBlock | None, FocusBlock | None]:
    """Find currently active `FocusBlock` and next upcoming `FocusBlock`."""
    active = next((b for b in blocks if b.is_active_at(utc_now)), None)
    upcoming = next((b for b in blocks if b.started_at > utc_now), None)
    return active, upcoming


def _compute_window_insight(
    conn: sqlite3.Connection,
    ledger: Ledger,
    horizon: str,
    horizons: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    utc_now: datetime,
    approaching_deadline_hours: int,
) -> HorizonInsight:
    """Aggregate timers, agenda queue, workplaces, and attributions over `[start, end)`."""
    active_interval, active_activity, elapsed = _resolve_active_timer(ledger, utc_now)
    blocks = tuple(list_focus_blocks(conn, since=window_start, until=window_end))
    active_block, upcoming_block = _split_focus_blocks(blocks, utc_now)
    intervals = ledger.list_intervals(since=window_start, until=window_end, include_open=True)
    act_map = {act.id: act for act in ledger.list_activities(include_archived=True)}
    totals_by_act, unallocated_secs, total_secs = _accumulate_window_seconds(
        intervals, window_start, window_end, utc_now
    )
    queue = _load_open_queue(
        conn, horizons, act_map, totals_by_act, utc_now, approaching_deadline_hours
    )
    day_start, _ = _today_bounds(utc_now)
    return HorizonInsight(
        horizon=horizon,
        reference_time=utc_now,
        date_iso=day_start.date().isoformat(),
        window_start=window_start,
        window_end=window_end,
        active_interval=active_interval,
        active_activity=active_activity,
        active_elapsed_seconds=elapsed,
        active_focus_block=active_block,
        upcoming_focus_block=upcoming_block,
        focus_blocks=blocks,
        total_tracked_seconds=total_secs,
        unallocated_seconds=unallocated_secs,
        planned_budget_seconds=sum(e.item.budget_seconds or ZERO_SECONDS_INT for e in queue),
        activity_totals=_build_activity_summaries(
            intervals, act_map, window_start, window_end, utc_now
        ),
        agenda_queue=queue,
        approaching_deadlines=tuple(e for e in queue if e.deadline_approaching or e.is_overdue),
        workplace=_resolve_current_workplace(ledger, active_interval, day_start),
        workplace_breakdown=_compute_workplace_breakdown(ledger, window_start, window_end),
        entity_attributions=tuple(
            ledger.query_entity_attribution(since=window_start, until=window_end)
        ),
    )


def _load_open_queue(
    conn: sqlite3.Connection,
    horizons: Sequence[str],
    act_map: Mapping[str, Activity],
    totals_by_act: Mapping[str, int],
    utc_now: datetime,
    approaching_deadline_hours: int,
) -> tuple[AgendaQueueEntry, ...]:
    """Load open `AgendaItems` for `horizons` and enrich with budget/deadline status."""
    raw_items = [
        item
        for item in list_agenda_items(conn, horizons=horizons, include_done=False)
        if item.status in OPEN_AGENDA_STATUSES
    ]
    return tuple(
        _build_queue_entries(raw_items, act_map, totals_by_act, utc_now, approaching_deadline_hours)
    )


def _clipped_interval_seconds(
    interval: Interval,
    window_start: datetime,
    window_end: datetime,
    utc_now: datetime,
) -> int:
    """Clip an open or closed `Interval` to `[window_start, window_end)` in seconds."""
    eff_start = max(interval.started_at, window_start)
    raw_end = utc_now if interval.ended_at is None else interval.ended_at
    eff_end = min(raw_end, window_end)
    if eff_end <= eff_start:
        return ZERO_SECONDS_INT
    return round((eff_end - eff_start).total_seconds())


def _accumulate_window_seconds(
    intervals: Sequence[Interval],
    window_start: datetime,
    window_end: datetime,
    utc_now: datetime,
) -> tuple[dict[str, int], int, int]:
    """Sum clipped `Interval` durations per `activity_id`, unallocated, and overall."""
    by_act: dict[str, int] = {}
    unallocated = ZERO_SECONDS_INT
    total = ZERO_SECONDS_INT
    for item in intervals:
        secs = _clipped_interval_seconds(item, window_start, window_end, utc_now)
        if secs <= ZERO_SECONDS_INT:
            continue
        total += secs
        if item.activity_id is None:
            unallocated += secs
        else:
            by_act[item.activity_id] = by_act.get(item.activity_id, ZERO_SECONDS_INT) + secs
    return by_act, unallocated, total


def _build_activity_summaries(
    intervals: Sequence[Interval],
    act_map: Mapping[str, Activity],
    window_start: datetime,
    window_end: datetime,
    utc_now: datetime,
) -> tuple[ActivityDurationSummary, ...]:
    """Build sorted `ActivityDurationSummary` rows across `[window_start, window_end)`."""
    secs_map: dict[str | None, int] = {}
    counts_map: dict[str | None, int] = {}
    for interval in intervals:
        secs = _clipped_interval_seconds(interval, window_start, window_end, utc_now)
        if secs <= ZERO_SECONDS_INT:
            continue
        key = interval.activity_id
        secs_map[key] = secs_map.get(key, ZERO_SECONDS_INT) + secs
        counts_map[key] = counts_map.get(key, ZERO_SECONDS_INT) + SINGLE_DAY

    rows = [
        summary
        for act_id, secs in secs_map.items()
        if (summary := _make_activity_summary(act_id, secs, counts_map[act_id], act_map))
        is not None
    ]
    rows.sort(key=lambda r: (-r.tracked_seconds, r.activity_key))
    return tuple(rows)


def _make_activity_summary(
    act_id: str | None,
    secs: int,
    count: int,
    act_map: Mapping[str, Activity],
) -> ActivityDurationSummary | None:
    """Create a single `ActivityDurationSummary` row for `act_id` or `None` if unknown."""
    if act_id is None:
        return ActivityDurationSummary(
            activity_id=None,
            activity_key=UNALLOCATED_ACTIVITY_KEY,
            activity_name=UNALLOCATED_ACTIVITY_NAME,
            activity_kind=None,
            tracked_seconds=secs,
            interval_count=count,
        )
    act = act_map.get(act_id)
    if act is None:
        return None
    return ActivityDurationSummary(
        activity_id=act.id,
        activity_key=activity_display_key(act),
        activity_name=act.name,
        activity_kind=act.kind,
        tracked_seconds=secs,
        interval_count=count,
    )


def _build_queue_entries(
    items: Sequence[AgendaItem],
    act_map: Mapping[str, Activity],
    totals_by_act: Mapping[str, int],
    utc_now: datetime,
    approaching_deadline_hours: int,
) -> list[AgendaQueueEntry]:
    """Enrich `AgendaItem` rows with tracked durations, remaining budgets, and deadlines."""
    threshold = utc_now + timedelta(hours=approaching_deadline_hours)
    entries: list[AgendaQueueEntry] = []
    for item in items:
        if (act := act_map.get(item.activity_id)) is None:
            continue
        tracked = totals_by_act.get(act.id, ZERO_SECONDS_INT)
        rem = (
            None
            if item.budget_seconds is None
            else max(ZERO_SECONDS_INT, item.budget_seconds - tracked)
        )
        dl = item.deadline_at
        entries.append(
            AgendaQueueEntry(
                item=item,
                activity=act,
                tracked_seconds=tracked,
                remaining_budget_seconds=rem,
                deadline_approaching=dl is not None and dl <= threshold,
                is_overdue=dl is not None and dl < utc_now,
            )
        )
    return entries


def _resolve_current_workplace(
    ledger: Ledger,
    active_interval: Interval | None,
    day_start: datetime,
) -> str | None:
    """Resolve active interval or current calendar day `workplace` `ContextFact`."""
    if active_interval is not None:
        int_fact = ledger.get_context_fact(
            BUILTIN_WORKPLACE_FACET_KEY, interval_id=active_interval.id
        )
        if int_fact is not None:
            return str(int_fact.value)
    date_fact = ledger.get_context_fact(BUILTIN_WORKPLACE_FACET_KEY, scope_date=day_start.date())
    return None if date_fact is None else str(date_fact.value)


def _compute_workplace_breakdown(
    ledger: Ledger,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, int]:
    """Count daily `workplace` `ContextFacts` across `[window_start, window_end)`."""
    counts: dict[str, int] = dict.fromkeys(BUILTIN_WORKPLACE_VALUES, ZERO_SECONDS_INT)
    inclusive_end = (window_end - timedelta(days=SINGLE_DAY)).date()
    facts = ledger.list_context_facts(
        facet_key=BUILTIN_WORKPLACE_FACET_KEY,
        since_date=window_start.date(),
        until_date=inclusive_end,
    )
    for fact in facts:
        if fact.scope_date is not None:
            val_str = str(fact.value)
            counts[val_str] = counts.get(val_str, ZERO_SECONDS_INT) + SINGLE_DAY
    return counts


def _focus_block_key(block: FocusBlock, act_lookup: Mapping[str, Activity]) -> str:
    """Resolve `activity_key` for an IPC `FocusBlockSummary`."""
    if block.activity_id is not None and block.activity_id in act_lookup:
        return activity_display_key(act_lookup[block.activity_id])
    return block.label


# endregion
