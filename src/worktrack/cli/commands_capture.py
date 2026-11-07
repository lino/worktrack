"""Subparser builders and command handlers for `wt in/out/log/split/fact`."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from worktrack.agenda.validators import (
    activity_display_key,
    serialize_activity,
    serialize_interval,
)
from worktrack.cli.context import (
    EXIT_SUCCESS,
    SUBCOMMAND_FACT,
    SUBCOMMAND_IN,
    SUBCOMMAND_LOG,
    SUBCOMMAND_OUT,
    SUBCOMMAND_SPLIT,
    UNALLOCATED_SLICE_TOKENS,
    CliContext,
    coerce_cli_facet_value,
    parse_cli_timestamp,
    resolve_or_create_activity,
    serialize_context_fact,
)
from worktrack.cli.render import write_json, write_line
from worktrack.core.exceptions import LedgerValidationError
from worktrack.core.ledger import Ledger
from worktrack.core.models import (
    ACTIVITY_KIND_TARGETED,
    ALLOWED_ACTIVITY_KINDS,
    ALLOWED_OVERLAP_POLICIES,
    OVERLAP_POLICY_FORBID,
    IntervalSlice,
)
from worktrack.ipc.prompt import format_duration_compact

# region Constants
MIN_POSITIVE_SLICE_MINUTES: Final = 0.0
KEY_VALUE_SEPARATOR: Final = "="
SLICE_SEPARATOR: Final = ":"
# endregion


# region Subcommands
def register_capture_subparsers(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `in`, `out`, `log`, `split`, and `fact` CLI subcommands."""
    _register_in_parser(subparsers, json_parent)
    _register_out_parser(subparsers, json_parent)
    _register_log_parser(subparsers, json_parent)
    _register_split_parser(subparsers, json_parent)
    _register_fact_parser(subparsers, json_parent)


def _register_in_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt in` subparser."""
    parser = subparsers.add_parser(
        SUBCOMMAND_IN,
        parents=[json_parent],
        help="Clock in to an Activity (resolving or creating on the fly).",
    )
    parser.add_argument("activity", help="Activity ID, external_ref, or exact name.")
    parser.add_argument(
        "--kind",
        default=ACTIVITY_KIND_TARGETED,
        choices=sorted(ALLOWED_ACTIVITY_KINDS),
        help="Activity kind when creating on the fly ('targeted' or 'standing').",
    )
    parser.add_argument("--ref", default=None, help="Optional external reference (e.g. EXT-1).")
    parser.add_argument("--notes", default=None, help="Optional interval notes.")
    parser.add_argument(
        "--policy",
        default=OVERLAP_POLICY_FORBID,
        choices=sorted(ALLOWED_OVERLAP_POLICIES),
        help="Overlap policy for open intervals ('forbid', 'close_previous', 'allow').",
    )
    parser.add_argument(
        "--tag",
        dest="tags",
        action="append",
        default=[],
        help="Repeatable freeform interval tag.",
    )
    parser.add_argument("--at", default=None, help="Optional ISO-8601 UTC start timestamp.")
    parser.set_defaults(handler=handle_in, canonical_subcommand=SUBCOMMAND_IN)


def _register_out_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt out` subparser."""
    parser = subparsers.add_parser(
        SUBCOMMAND_OUT,
        parents=[json_parent],
        help="Clock out of the active open Interval.",
    )
    parser.add_argument("--notes", default=None, help="Optional closing interval notes.")
    parser.add_argument(
        "--tag",
        dest="tags",
        action="append",
        default=[],
        help="Optional replacement interval tags.",
    )
    parser.add_argument("--at", default=None, help="Optional ISO-8601 UTC end timestamp.")
    parser.set_defaults(handler=handle_out, canonical_subcommand=SUBCOMMAND_OUT)


def _register_log_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt log` chaotic/retroactive capture subparser."""
    parser = subparsers.add_parser(
        SUBCOMMAND_LOG,
        parents=[json_parent],
        help="Record a chaotic unallocated or retroactively bound Interval.",
    )
    parser.add_argument("--start", required=True, help="Start timestamp (ISO-8601 or HH:MM).")
    parser.add_argument("--end", required=True, help="End timestamp (ISO-8601 or HH:MM).")
    parser.add_argument("--activity", default=None, help="Optional Activity ID, ref, or name.")
    parser.add_argument(
        "--kind",
        default=ACTIVITY_KIND_TARGETED,
        choices=sorted(ALLOWED_ACTIVITY_KINDS),
        help="Activity kind if created on the fly.",
    )
    parser.add_argument("--notes", default=None, help="Optional interval notes.")
    parser.add_argument(
        "--tag",
        dest="tags",
        action="append",
        default=[],
        help="Repeatable freeform interval tag.",
    )
    parser.set_defaults(handler=handle_log, canonical_subcommand=SUBCOMMAND_LOG)


def _register_split_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt split` subparser."""
    parser = subparsers.add_parser(
        SUBCOMMAND_SPLIT,
        parents=[json_parent],
        help="Split a closed Interval into contiguous '<activity>:<minutes>' slices.",
    )
    parser.add_argument("interval_id", help="Closed Interval identifier to split.")
    parser.add_argument(
        "--slice",
        dest="slices",
        action="append",
        required=True,
        help="Slice specification '<activity_or_unallocated>:<minutes>' (repeatable, >= 2).",
    )
    parser.set_defaults(handler=handle_split, canonical_subcommand=SUBCOMMAND_SPLIT)


def _register_fact_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt fact` subparser."""
    parser = subparsers.add_parser(
        SUBCOMMAND_FACT,
        parents=[json_parent],
        help="Assert a date-scoped or interval-scoped Facet ContextFact.",
    )
    parser.add_argument("assignment", help="Key/value assignment '<facet_key>=<value>'.")
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--date", default=None, help="Target date 'YYYY-MM-DD' (default today)."
    )
    scope_group.add_argument("--interval", default=None, help="Target Interval ID.")
    parser.set_defaults(handler=handle_fact, canonical_subcommand=SUBCOMMAND_FACT)


# endregion


# region CLI
def handle_in(ctx: CliContext, args: argparse.Namespace) -> int:
    """Execute `wt in` to start a structured `Interval`."""
    started_at = None if args.at is None else parse_cli_timestamp(args.at, field_name="at")
    with ctx.open_ledger() as ledger:
        activity = resolve_or_create_activity(
            ledger,
            str(args.activity),
            kind=str(args.kind),
            external_ref=args.ref,
        )
        interval = ledger.clock_in(
            activity.id,
            started_at=started_at,
            notes=args.notes,
            tags=list(args.tags),
            overlap_policy=str(args.policy),
        )
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "activity": serialize_activity(activity),
                "interval": serialize_interval(interval),
            },
        )
        return EXIT_SUCCESS
    write_line(
        ctx.stdout,
        f"Clocked in: {activity_display_key(activity)} ({activity.kind}) [{interval.id}]",
    )
    return EXIT_SUCCESS


def handle_out(ctx: CliContext, args: argparse.Namespace) -> int:
    """Execute `wt out` to close the active open `Interval`."""
    ended_at = None if args.at is None else parse_cli_timestamp(args.at, field_name="at")
    tags_arg = list(args.tags) if args.tags else None
    with ctx.open_ledger() as ledger:
        interval = ledger.clock_out(ended_at=ended_at, notes=args.notes, tags=tags_arg)
        activity = (
            None if interval.activity_id is None else ledger.get_activity(interval.activity_id)
        )
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "interval": serialize_interval(interval),
                "activity": None if activity is None else serialize_activity(activity),
            },
        )
        return EXIT_SUCCESS
    dur = format_duration_compact(round(interval.duration_seconds or 0.0))
    act_label = activity_display_key(activity) if activity is not None else "unallocated"
    write_line(ctx.stdout, f"Clocked out: {act_label} ({dur}) [{interval.id}]")
    return EXIT_SUCCESS


def handle_log(ctx: CliContext, args: argparse.Namespace) -> int:
    """Execute `wt log` to record a chaotic `Interval`."""
    started_at = parse_cli_timestamp(str(args.start), field_name="start")
    ended_at = parse_cli_timestamp(str(args.end), field_name="end")
    with ctx.open_ledger() as ledger:
        activity = None
        if args.activity is not None:
            activity = resolve_or_create_activity(
                ledger,
                str(args.activity),
                kind=str(args.kind),
            )
        interval = ledger.record_chaotic_interval(
            started_at=started_at,
            ended_at=ended_at,
            notes=args.notes,
            tags=list(args.tags),
            activity_id=None if activity is None else activity.id,
        )
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "interval": serialize_interval(interval),
                "activity": None if activity is None else serialize_activity(activity),
            },
        )
        return EXIT_SUCCESS
    dur = format_duration_compact(round(interval.duration_seconds or 0.0))
    target_label = activity_display_key(activity) if activity is not None else "unallocated"
    write_line(ctx.stdout, f"Logged chaotic interval [{interval.id}]: {dur} -> {target_label}")
    return EXIT_SUCCESS


def handle_split(ctx: CliContext, args: argparse.Namespace) -> int:
    """Execute `wt split` to divide a closed `Interval` across slices."""
    with ctx.open_ledger() as ledger:
        specs = [_parse_slice_spec(ledger, str(raw)) for raw in args.slices]
        children = ledger.split_interval(str(args.interval_id), specs)
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "parent_interval_id": str(args.interval_id),
                "slices": [serialize_interval(child) for child in children],
            },
        )
        return EXIT_SUCCESS
    write_line(
        ctx.stdout,
        f"Split interval {args.interval_id} into {len(children)} slices:",
    )
    for child in children:
        dur = format_duration_compact(round(child.duration_seconds or 0.0))
        write_line(ctx.stdout, f"  - [{child.id}] {dur} (activity={child.activity_id or 'none'})")
    return EXIT_SUCCESS


def _parse_slice_spec(ledger: Ledger, raw_spec: str) -> IntervalSlice:
    """Parse `<activity_or_unallocated>:<minutes>` into a validated `IntervalSlice`."""
    if SLICE_SEPARATOR not in raw_spec:
        raise LedgerValidationError(f"Slice '{raw_spec}' must match '<activity>:<minutes>'.")
    raw_act, raw_minutes = raw_spec.rsplit(SLICE_SEPARATOR, 1)
    try:
        minutes = float(raw_minutes.strip())
    except ValueError as exc:
        raise LedgerValidationError(
            f"Slice minutes in '{raw_spec}' must be a positive number."
        ) from exc
    if minutes <= MIN_POSITIVE_SLICE_MINUTES:
        raise LedgerValidationError("Slice minutes must be > 0.")

    act_token = raw_act.strip()
    if act_token.lower() in UNALLOCATED_SLICE_TOKENS:
        act_id = None
    else:
        act_id = resolve_or_create_activity(ledger, act_token).id
    return IntervalSlice(activity_id=act_id, duration=timedelta(minutes=minutes))


def handle_fact(ctx: CliContext, args: argparse.Namespace) -> int:
    """Execute `wt fact` to assert a scoped `ContextFact`."""
    raw_assign = str(args.assignment)
    if KEY_VALUE_SEPARATOR not in raw_assign:
        raise LedgerValidationError(f"Fact assignment '{raw_assign}' must match '<key>=<value>'.")
    raw_key, raw_val = raw_assign.split(KEY_VALUE_SEPARATOR, 1)
    scope_date: str | None = args.date
    interval_id: str | None = args.interval
    if scope_date is None and interval_id is None:
        scope_date = datetime.now(UTC).date().isoformat()

    with ctx.open_ledger() as ledger:
        facet = ledger.get_facet(raw_key.strip())
        coerced = coerce_cli_facet_value(facet, raw_val)
        fact = ledger.assert_context_fact(
            facet.key,
            coerced,
            scope_date=scope_date,
            interval_id=interval_id,
        )
    if ctx.as_json:
        write_json(ctx.stdout, {"fact": serialize_context_fact(fact)})
        return EXIT_SUCCESS
    scope_label = fact.scope_date if fact.scope_date is not None else f"interval:{fact.interval_id}"
    write_line(ctx.stdout, f"Asserted fact: {fact.facet_key}={fact.value} ({scope_label})")
    return EXIT_SUCCESS


# endregion
