"""Subparser builders and command handlers for `wt now/today/week` and `wt agenda`."""

from __future__ import annotations

import argparse
from typing import Any, Final

from worktrack.agenda.models import (
    ALLOWED_AGENDA_HORIZONS,
    ALLOWED_AGENDA_STATUSES,
    DEFAULT_AGENDA_PRIORITY,
    DEFAULT_FOCUS_LABEL,
    HORIZON_TODAY,
    OPEN_AGENDA_STATUSES,
    AgendaItem,
    AgendaItemNotFoundError,
)
from worktrack.agenda.planner import AgendaPlanner
from worktrack.agenda.validators import activity_display_key, serialize_activity
from worktrack.cli.context import (
    EXIT_SUCCESS,
    SUBCOMMAND_AGENDA,
    SUBCOMMAND_NOW,
    SUBCOMMAND_TODAY,
    SUBCOMMAND_WEEK,
    CliContext,
    parse_cli_timestamp,
    resolve_or_create_activity,
)
from worktrack.cli.render import (
    render_agenda_list,
    render_horizon_insight,
    render_now_insight,
    write_json,
    write_line,
)
from worktrack.core.exceptions import ActivityNotFoundError
from worktrack.core.ledger import Ledger
from worktrack.core.models import ACTIVITY_KIND_TARGETED, ALLOWED_ACTIVITY_KINDS

# region Constants
AGENDA_ACTION_ADD: Final = "add"
AGENDA_ACTION_LIST: Final = "list"
AGENDA_ACTION_FOCUS: Final = "focus"
AGENDA_ACTION_DONE: Final = "done"
# endregion


# region Subcommands
def register_planning_subparsers(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `now`, `today`, `week`, and `agenda` CLI subcommands."""
    for name, handler, desc in (
        (SUBCOMMAND_NOW, handle_now, "Show active timer, focus block, and immediate agenda."),
        (SUBCOMMAND_TODAY, handle_today, "Show Today's tracked totals, agenda, and attributions."),
        (SUBCOMMAND_WEEK, handle_week, "Show This Week's tracked totals, workplaces, and shares."),
    ):
        hp = subparsers.add_parser(name, parents=[json_parent], help=desc)
        hp.add_argument("--now", default=None, help="Optional reference UTC timestamp override.")
        hp.set_defaults(handler=handler, canonical_subcommand=name)

    _register_agenda_parser(subparsers, json_parent)


def _register_agenda_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt agenda {add,list,focus,done}` nested subparsers."""
    agenda_parser = subparsers.add_parser(
        SUBCOMMAND_AGENDA,
        parents=[json_parent],
        help="Manage Horizon Agenda items and scheduled FocusBlocks.",
    )
    agenda_parser.set_defaults(handler=handle_agenda, canonical_subcommand=SUBCOMMAND_AGENDA)
    nested = agenda_parser.add_subparsers(dest="agenda_action", required=True)
    _register_agenda_add_and_list(nested, json_parent)
    _register_agenda_focus_and_done(nested, json_parent)


def _register_agenda_add_and_list(
    nested: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt agenda add` and `wt agenda list` subparsers."""
    add_p = nested.add_parser(
        AGENDA_ACTION_ADD,
        parents=[json_parent],
        help="Schedule an AgendaItem for an Activity.",
    )
    add_p.add_argument("activity", help="Activity ID, external_ref, or name.")
    add_p.add_argument(
        "--horizon",
        default=HORIZON_TODAY,
        choices=sorted(ALLOWED_AGENDA_HORIZONS),
        help="Planning horizon ('today', 'this_week', 'backlog').",
    )
    add_p.add_argument(
        "--priority",
        type=int,
        default=DEFAULT_AGENDA_PRIORITY,
        help="Numeric urgency priority (lower runs first).",
    )
    add_p.add_argument(
        "--budget-minutes",
        type=int,
        default=None,
        help="Optional daily/horizon budget in minutes.",
    )
    add_p.add_argument("--deadline", default=None, help="Optional deadline (YYYY-MM-DD or ISO).")
    add_p.add_argument(
        "--kind",
        default=ACTIVITY_KIND_TARGETED,
        choices=sorted(ALLOWED_ACTIVITY_KINDS),
        help="Activity kind if created on the fly.",
    )
    add_p.add_argument("--notes", default=None, help="Optional planning notes.")

    list_p = nested.add_parser(
        AGENDA_ACTION_LIST,
        parents=[json_parent],
        help="List scheduled AgendaItems and FocusBlocks.",
    )
    list_p.add_argument(
        "--horizon",
        default=None,
        choices=sorted(ALLOWED_AGENDA_HORIZONS),
        help="Filter by planning horizon.",
    )
    list_p.add_argument(
        "--status",
        default=None,
        choices=sorted(ALLOWED_AGENDA_STATUSES),
        help="Filter by item status.",
    )


def _register_agenda_focus_and_done(
    nested: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt agenda focus` and `wt agenda done` subparsers."""
    focus_p = nested.add_parser(
        AGENDA_ACTION_FOCUS,
        parents=[json_parent],
        help="Schedule a timeboxed FocusBlock.",
    )
    focus_p.add_argument("--start", required=True, help="Start timestamp (ISO-8601 or HH:MM).")
    focus_p.add_argument("--end", required=True, help="End timestamp (ISO-8601 or HH:MM).")
    focus_p.add_argument("--activity", default=None, help="Optional bound Activity ID/ref/name.")
    focus_p.add_argument("--label", default=None, help="FocusBlock label.")
    focus_p.add_argument(
        "--expose-busy",
        action="store_true",
        help="Opt in to exposing free/busy block externally without details.",
    )
    focus_p.add_argument("--notes", default=None, help="Optional focus block notes.")

    done_p = nested.add_parser(
        AGENDA_ACTION_DONE,
        parents=[json_parent],
        help="Mark an AgendaItem as done (by item ID or Activity key/name).",
    )
    done_p.add_argument("target", help="AgendaItem ID or bound Activity ID/ref/name.")


# endregion


# region CLI
def handle_now(ctx: CliContext, args: argparse.Namespace) -> int:
    """Execute `wt now` insight query."""
    ref_now = None if args.now is None else parse_cli_timestamp(args.now, field_name="now")
    with ctx.open_ledger() as ledger, ctx.open_planner(ledger=ledger) as planner:
        insight = planner.get_now_insight(now=ref_now, ledger=ledger)
    if ctx.as_json:
        write_json(ctx.stdout, insight.to_dict())
    else:
        render_now_insight(ctx.stdout, insight)
    return EXIT_SUCCESS


def handle_today(ctx: CliContext, args: argparse.Namespace) -> int:
    """Execute `wt today` insight query."""
    ref_now = None if args.now is None else parse_cli_timestamp(args.now, field_name="now")
    with ctx.open_ledger() as ledger, ctx.open_planner(ledger=ledger) as planner:
        insight = planner.get_today_insight(now=ref_now, ledger=ledger)
    if ctx.as_json:
        write_json(ctx.stdout, insight.to_dict())
    else:
        render_horizon_insight(ctx.stdout, insight)
    return EXIT_SUCCESS


def handle_week(ctx: CliContext, args: argparse.Namespace) -> int:
    """Execute `wt week` insight query."""
    ref_now = None if args.now is None else parse_cli_timestamp(args.now, field_name="now")
    with ctx.open_ledger() as ledger, ctx.open_planner(ledger=ledger) as planner:
        insight = planner.get_week_insight(now=ref_now, ledger=ledger)
    if ctx.as_json:
        write_json(ctx.stdout, insight.to_dict())
    else:
        render_horizon_insight(ctx.stdout, insight)
    return EXIT_SUCCESS


def handle_agenda(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch `wt agenda {add,list,focus,done}` subcommands."""
    action = str(args.agenda_action)
    if action == AGENDA_ACTION_ADD:
        return _handle_agenda_add(ctx, args)
    if action == AGENDA_ACTION_LIST:
        return _handle_agenda_list(ctx, args)
    if action == AGENDA_ACTION_FOCUS:
        return _handle_agenda_focus(ctx, args)
    return _handle_agenda_done(ctx, args)


def _handle_agenda_add(ctx: CliContext, args: argparse.Namespace) -> int:
    """Schedule a new `AgendaItem`."""
    with ctx.open_ledger() as ledger, ctx.open_planner(ledger=ledger) as planner:
        activity = resolve_or_create_activity(
            ledger,
            str(args.activity),
            kind=str(args.kind),
        )
        item = planner.schedule_item(
            activity.id,
            horizon=str(args.horizon),
            priority=int(args.priority),
            daily_budget_minutes=args.budget_minutes,
            deadline=args.deadline,
            notes=args.notes,
        )
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {"item": item.to_dict(), "activity": serialize_activity(activity)},
        )
        return EXIT_SUCCESS
    write_line(
        ctx.stdout,
        f"Scheduled agenda item [{item.id}]: P{item.priority} ({item.horizon}) "
        f"-> {activity_display_key(activity)}",
    )
    return EXIT_SUCCESS


def _handle_agenda_list(ctx: CliContext, args: argparse.Namespace) -> int:
    """List scheduled `AgendaItems` and `FocusBlocks`."""
    with ctx.open_ledger() as ledger, ctx.open_planner(ledger=ledger) as planner:
        items = planner.list_items(horizon=args.horizon, status=args.status, include_done=True)
        blocks = planner.list_focus_blocks()
        act_map = {act.id: act for act in ledger.list_activities(include_archived=True)}
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "items": [item.to_dict() for item in items],
                "focus_blocks": [block.to_dict() for block in blocks],
            },
        )
        return EXIT_SUCCESS
    render_agenda_list(ctx.stdout, items, blocks, act_map)
    return EXIT_SUCCESS


def _handle_agenda_focus(ctx: CliContext, args: argparse.Namespace) -> int:
    """Schedule a timeboxed `FocusBlock`."""
    started_at = parse_cli_timestamp(str(args.start), field_name="start")
    ended_at = parse_cli_timestamp(str(args.end), field_name="end")
    with ctx.open_ledger() as ledger, ctx.open_planner(ledger=ledger) as planner:
        act_id: str | None = None
        default_label = DEFAULT_FOCUS_LABEL
        if args.activity is not None:
            act = resolve_or_create_activity(ledger, str(args.activity))
            act_id = act.id
            default_label = act.name
        block = planner.schedule_focus_block(
            started_at=started_at,
            ended_at=ended_at,
            activity_id=act_id,
            label=default_label if args.label is None else str(args.label),
            expose_busy=bool(args.expose_busy),
            notes=args.notes,
        )
    if ctx.as_json:
        write_json(ctx.stdout, {"focus_block": block.to_dict()})
        return EXIT_SUCCESS
    write_line(ctx.stdout, f"Scheduled focus block [{block.id}]: {block.label}")
    return EXIT_SUCCESS


def _handle_agenda_done(ctx: CliContext, args: argparse.Namespace) -> int:
    """Mark an `AgendaItem` as `done` by item ID or bound `Activity` identifier."""
    target = str(args.target).strip()
    with ctx.open_ledger() as ledger, ctx.open_planner(ledger=ledger) as planner:
        item = _resolve_target_agenda_item(ledger, planner, target)
        completed = planner.complete_item(item.id)
    if ctx.as_json:
        write_json(ctx.stdout, {"item": completed.to_dict()})
        return EXIT_SUCCESS
    write_line(ctx.stdout, f"Completed agenda item [{completed.id}].")
    return EXIT_SUCCESS


def _resolve_target_agenda_item(
    ledger: Ledger,
    planner: AgendaPlanner,
    target: str,
) -> AgendaItem:
    """Resolve `target` as an exact `AgendaItem.id` or open item for `Activity`."""
    try:
        return planner.get_item(target)
    except AgendaItemNotFoundError:
        return _resolve_open_item_by_activity(ledger, planner, target)


def _resolve_open_item_by_activity(
    ledger: Ledger,
    planner: AgendaPlanner,
    target: str,
) -> AgendaItem:
    """Resolve the highest-priority open `AgendaItem` scheduled for `target` `Activity`."""
    try:
        act = resolve_or_create_activity(ledger, target, create_if_missing=False)
    except ActivityNotFoundError as exc:
        raise AgendaItemNotFoundError(f"No AgendaItem or Activity matching '{target}'.") from exc
    candidates = [
        entry
        for entry in planner.list_items(activity_id=act.id, include_done=False)
        if entry.status in OPEN_AGENDA_STATUSES
    ]
    if not candidates:
        raise AgendaItemNotFoundError(
            f"No open AgendaItem scheduled for Activity '{activity_display_key(act)}'."
        )
    return candidates[0]


# endregion
