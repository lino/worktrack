"""Human-friendly terminal and `--json` output formatters for `wt` CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final, TextIO

from worktrack.agenda.models import (
    AgendaItem,
    AgendaQueueEntry,
    FocusBlock,
    HorizonInsight,
    NowInsight,
)
from worktrack.agenda.validators import activity_display_key
from worktrack.core.models import Activity, format_utc_iso
from worktrack.ipc.models import DaemonStatusSnapshot
from worktrack.ipc.prompt import format_duration_compact

# region Constants
PERCENT_MULTIPLIER: Final = 100.0
# endregion


# region Rendering
def write_line(stream: TextIO, text: str = "") -> None:
    """Write a single newline-terminated line to an injectable output `stream`."""
    stream.write(f"{text}\n")


def write_json(stream: TextIO, payload: Mapping[str, object]) -> None:
    """Serialize `payload` as compact newline-terminated JSON to `stream`."""
    stream.write(f"{json.dumps(dict(payload), sort_keys=False)}\n")


def render_now_insight(stream: TextIO, insight: NowInsight) -> None:
    """Render concise hacker summary for `wt now`."""
    today_str = format_duration_compact(insight.today_total_seconds)
    write_line(stream, f"=== Now ({insight.date_iso}) | today {today_str} ===")
    if insight.active_activity is not None:
        key = activity_display_key(insight.active_activity)
        elapsed = format_duration_compact(insight.active_elapsed_seconds)
        write_line(
            stream,
            f"active: {key} — {insight.active_activity.name} ({elapsed})",
        )
    else:
        write_line(stream, "active: idle")

    if insight.workplace is not None:
        write_line(stream, f"workplace: {insight.workplace}")
    if insight.active_focus_block is not None:
        write_line(stream, f"focus: {_format_focus_block_brief(insight.active_focus_block)} [now]")
    elif insight.upcoming_focus_block is not None:
        write_line(
            stream,
            f"next focus: {_format_focus_block_brief(insight.upcoming_focus_block)}",
        )
    if insight.next_agenda_item is not None:
        write_line(stream, f"top agenda: {_format_queue_entry(insight.next_agenda_item)}")


def render_horizon_insight(stream: TextIO, insight: HorizonInsight) -> None:
    """Render concise hacker summary for `wt today` or `wt week`."""
    total_str = format_duration_compact(insight.total_tracked_seconds)
    budget_str = format_duration_compact(insight.planned_budget_seconds)
    write_line(
        stream,
        f"=== Horizon: {insight.horizon} ({insight.date_iso}) | "
        f"tracked {total_str} / budget {budget_str} ===",
    )
    if insight.active_activity is not None:
        key = activity_display_key(insight.active_activity)
        elapsed = format_duration_compact(insight.active_elapsed_seconds)
        write_line(stream, f"active: {key} ({elapsed})")
    if insight.workplace is not None:
        write_line(stream, f"workplace: {insight.workplace}")

    _render_workplace_breakdown(stream, insight.workplace_breakdown)
    _render_activity_totals(stream, insight)
    _render_agenda_queue(stream, insight.agenda_queue)
    _render_focus_blocks_section(stream, insight.focus_blocks)
    _render_attributions_section(stream, insight)


def _render_workplace_breakdown(stream: TextIO, breakdown: Mapping[str, int]) -> None:
    """Render non-zero daily `workplace` counts."""
    non_zero = [f"{k}={v}d" for k, v in sorted(breakdown.items()) if v > 0]
    if non_zero:
        write_line(stream, f"workplaces: {', '.join(non_zero)}")


def _render_activity_totals(stream: TextIO, insight: HorizonInsight) -> None:
    """Render tracked duration lines per `Activity`."""
    if not insight.activity_totals:
        write_line(stream, "activities: none tracked")
        return
    write_line(stream, "activities:")
    for summary in insight.activity_totals:
        dur = format_duration_compact(summary.tracked_seconds)
        write_line(stream, f"  - {summary.activity_key}: {dur} ({summary.interval_count}x)")


def _render_agenda_queue(stream: TextIO, queue: Sequence[AgendaQueueEntry]) -> None:
    """Render prioritized `Agenda` queue lines."""
    if not queue:
        write_line(stream, "agenda: empty")
        return
    write_line(stream, "agenda:")
    for entry in queue:
        write_line(stream, f"  - {_format_queue_entry(entry)}")


def _render_focus_blocks_section(stream: TextIO, blocks: Sequence[FocusBlock]) -> None:
    """Render scheduled `FocusBlock` lines when present."""
    if not blocks:
        return
    write_line(stream, "focus blocks:")
    for block in blocks:
        write_line(stream, f"  - {_format_focus_block_brief(block)}")


def _render_attributions_section(stream: TextIO, insight: HorizonInsight) -> None:
    """Render `Entity` `Attribution` rollup shares when present."""
    if not insight.entity_attributions:
        return
    write_line(stream, "attributions:")
    for share in insight.entity_attributions:
        dur = format_duration_compact(round(share.attributed_seconds))
        pct = share.share_of_kind * PERCENT_MULTIPLIER
        write_line(
            stream,
            f"  - {share.entity.kind}:{share.entity.key} ({share.entity.name}) {dur} [{pct:.0f}%]",
        )


def render_agenda_list(
    stream: TextIO,
    items: Sequence[AgendaItem],
    blocks: Sequence[FocusBlock],
    act_map: Mapping[str, Activity],
) -> None:
    """Render `wt agenda list` items and focus blocks."""
    if not items:
        write_line(stream, "agenda items: none")
    else:
        write_line(stream, "agenda items:")
        for item in items:
            act = act_map.get(item.activity_id)
            label = activity_display_key(act) if act is not None else item.activity_id
            budget = (
                f", budget {format_duration_compact(item.budget_seconds)}"
                if item.budget_seconds is not None
                else ""
            )
            due = f", due {item.deadline}" if item.deadline is not None else ""
            write_line(
                stream,
                f"  - [{item.status}] P{item.priority} ({item.horizon}) "
                f"{label} [{item.id}]{budget}{due}",
            )
    if blocks:
        write_line(stream, "focus blocks:")
        for block in blocks:
            write_line(stream, f"  - {_format_focus_block_brief(block)} [{block.id}]")


def render_status_human(
    stream: TextIO,
    *,
    online: bool,
    snapshot: DaemonStatusSnapshot,
) -> None:
    """Render `wt status` human-readable summary."""
    daemon_tag = "online" if online else "offline"
    if snapshot.is_tracking:
        label = snapshot.active_activity_key or snapshot.active_activity_title or "active"
        elapsed = format_duration_compact(snapshot.active_interval_elapsed_seconds)
        daily = format_duration_compact(snapshot.daily_total_seconds)
        wp = f" | {snapshot.workplace}" if snapshot.workplace else ""
        write_line(
            stream,
            f"wt: {label} {elapsed} | today {daily}{wp} (worktrackd: {daemon_tag})",
        )
        return
    daily = format_duration_compact(snapshot.daily_total_seconds)
    write_line(stream, f"wt: idle | today {daily} (worktrackd: {daemon_tag})")


def _format_queue_entry(entry: AgendaQueueEntry) -> str:
    """Format a single `AgendaQueueEntry` bullet line."""
    key = activity_display_key(entry.activity)
    logged = format_duration_compact(entry.tracked_seconds)
    parts = [f"[P{entry.item.priority}] {key} ({logged}"]
    if entry.item.budget_seconds is not None and entry.remaining_budget_seconds is not None:
        budget = format_duration_compact(entry.item.budget_seconds)
        rem = format_duration_compact(entry.remaining_budget_seconds)
        parts[0] = f"[P{entry.item.priority}] {key} ({logged}/{budget}, rem {rem}"
    parts[0] += ")"
    if entry.item.deadline is not None:
        flag = " OVERDUE" if entry.is_overdue else (" SOON" if entry.deadline_approaching else "")
        parts.append(f"due {entry.item.deadline}{flag}")
    return " ".join(parts)


def _format_focus_block_brief(block: FocusBlock) -> str:
    """Format a single `FocusBlock` range and busy exposure flag."""
    busy = " [busy]" if block.expose_busy else ""
    return (
        f"{block.label} ({format_utc_iso(block.started_at)} -> "
        f"{format_utc_iso(block.ended_at)}){busy}"
    )


# endregion
