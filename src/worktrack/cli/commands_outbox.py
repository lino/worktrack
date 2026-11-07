"""Subparser builders and command handlers for `wt outbox` staging, review, and egress."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final

from worktrack.cli.context import (
    EXIT_SUCCESS,
    SUBCOMMAND_OUTBOX,
    CliContext,
    parse_cli_timestamp,
)
from worktrack.cli.render import (
    render_egress_records_human,
    render_flush_results_human,
    render_outbox_items_human,
    serialize_egress_record,
    serialize_flush_result,
    serialize_outbox_item,
    write_json,
    write_line,
)
from worktrack.connectors import (
    JIRA_SINK_SLUG,
    JiraCloudConfig,
    JiraConfigurationError,
    JiraOAuthClient,
    JiraWorklogSink,
)
from worktrack.outbox import (
    ALLOWED_EGRESS_OUTCOMES,
    ALLOWED_FLUSH_CADENCES,
    ALLOWED_FLUSH_TRIGGERS,
    ALLOWED_GROUPING_WINDOWS,
    ALLOWED_OUTBOX_STATUSES,
    ALLOWED_ROUNDING_MODES,
    CHANGE_KIND_UNCHANGED,
    DEFAULT_FLUSH_CADENCE,
    DEFAULT_GROUPING_WINDOW,
    DEFAULT_MIN_DURATION_SECONDS,
    DEFAULT_ROUNDING_MODE,
    DEFAULT_ROUNDING_QUANTUM_SECONDS,
    FLUSH_TRIGGER_MANUAL,
    OutboxDispatcher,
    OutboxItem,
    RollupEngine,
    RollupPolicy,
    RollupSink,
)
from worktrack.secrets import UserTokenStore
from worktrack.telemetry.logging import StructuredEventLogger

# region Constants
OUTBOX_ACTION_PREVIEW: Final = "preview"
OUTBOX_ACTION_LIST: Final = "list"
OUTBOX_ACTION_APPROVE: Final = "approve"
OUTBOX_ACTION_REJECT: Final = "reject"
OUTBOX_ACTION_FLUSH: Final = "flush"
OUTBOX_ACTION_LEDGER: Final = "ledger"

_CLI_SINKS_OVERRIDE: ContextVar[tuple[RollupSink, ...] | None] = ContextVar(
    "worktrack_cli_sinks_override",
    default=None,
)
# endregion


# region Subcommands
@contextmanager
def use_cli_sinks(sinks: Iterable[RollupSink]) -> Iterator[None]:
    """Bind injectable `RollupSink` adapters for `wt outbox flush` within a context."""
    token = _CLI_SINKS_OVERRIDE.set(tuple(sinks))
    try:
        yield
    finally:
        _CLI_SINKS_OVERRIDE.reset(token)


def register_outbox_subparsers(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `wt outbox {preview,list,approve,reject,flush,ledger}` subparsers."""
    outbox_p = subparsers.add_parser(
        SUBCOMMAND_OUTBOX,
        parents=[json_parent],
        help="Preview, stage, approve, reject, flush, and audit Outbox egress rollups.",
    )
    outbox_p.set_defaults(handler=handle_outbox, canonical_subcommand=SUBCOMMAND_OUTBOX)
    nested = outbox_p.add_subparsers(dest="outbox_action", required=True)

    _register_preview_parser(nested, json_parent)
    _register_list_parser(nested, json_parent)
    _register_approve_reject_parsers(nested, json_parent)
    _register_flush_ledger_parsers(nested, json_parent)


def _register_preview_parser(
    nested: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt outbox preview` arguments."""
    prev_p = nested.add_parser(
        OUTBOX_ACTION_PREVIEW,
        parents=[json_parent],
        help="Compute redacted dry-run rollup diffs and optionally stage into Outbox.",
    )
    prev_p.add_argument("--sink", default=JIRA_SINK_SLUG, help="Target Sink slug (default: jira).")
    prev_p.add_argument("--stage", action="store_true", help="Stage actionable proposals.")
    prev_p.add_argument(
        "--cadence",
        default=DEFAULT_FLUSH_CADENCE,
        choices=sorted(ALLOWED_FLUSH_CADENCES),
        help="Flush cadence policy.",
    )
    prev_p.add_argument(
        "--grouping-window",
        default=DEFAULT_GROUPING_WINDOW,
        choices=sorted(ALLOWED_GROUPING_WINDOWS),
        help="Rollup grouping window.",
    )
    prev_p.add_argument(
        "--rounding-quantum",
        type=int,
        default=DEFAULT_ROUNDING_QUANTUM_SECONDS,
        help="Duration rounding quantum in seconds.",
    )
    prev_p.add_argument(
        "--rounding-mode",
        default=DEFAULT_ROUNDING_MODE,
        choices=sorted(ALLOWED_ROUNDING_MODES),
        help="Duration rounding mode.",
    )
    prev_p.add_argument(
        "--min-duration",
        type=int,
        default=DEFAULT_MIN_DURATION_SECONDS,
        help="Minimum bucket duration in seconds.",
    )
    prev_p.add_argument(
        "--include-notes",
        action="store_true",
        help="Opt in to include sanitized notes in summary.",
    )
    prev_p.add_argument("--since", default=None, help="Optional inclusive UTC start timestamp.")
    prev_p.add_argument("--until", default=None, help="Optional exclusive UTC end timestamp.")


def _register_list_parser(
    nested: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt outbox list` arguments."""
    list_p = nested.add_parser(
        OUTBOX_ACTION_LIST,
        parents=[json_parent],
        help="List staged, approved, rejected, failed, or flushed Outbox items.",
    )
    list_p.add_argument(
        "--status",
        default=None,
        choices=sorted(ALLOWED_OUTBOX_STATUSES),
        help="Optional Outbox status filter.",
    )
    list_p.add_argument("--sink", default=None, help="Optional Sink slug filter.")
    list_p.add_argument(
        "--cadence",
        default=None,
        choices=sorted(ALLOWED_FLUSH_CADENCES),
        help="Optional flush cadence filter.",
    )


def _register_approve_reject_parsers(
    nested: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt outbox approve` and `wt outbox reject` arguments."""
    approve_p = nested.add_parser(
        OUTBOX_ACTION_APPROVE,
        parents=[json_parent],
        help="Approve a staged or failed Outbox item for egress.",
    )
    approve_p.add_argument("item_id", help="OutboxItem UUID.")

    reject_p = nested.add_parser(
        OUTBOX_ACTION_REJECT,
        parents=[json_parent],
        help="Reject a staged, approved, or failed Outbox item.",
    )
    reject_p.add_argument("item_id", help="OutboxItem UUID.")
    reject_p.add_argument("--reason", default=None, help="Optional human rejection reason.")


def _register_flush_ledger_parsers(
    nested: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt outbox flush` and `wt outbox ledger` arguments."""
    flush_p = nested.add_parser(
        OUTBOX_ACTION_FLUSH,
        parents=[json_parent],
        help="Dispatch approved Outbox items to external Sinks and record EgressLedger.",
    )
    flush_p.add_argument("--item-id", default=None, help="Flush a single approved OutboxItem ID.")
    flush_p.add_argument("--sink", default=None, help="Optional Sink slug filter.")
    flush_p.add_argument(
        "--cadence",
        default=None,
        choices=sorted(ALLOWED_FLUSH_CADENCES),
        help="Optional cadence filter.",
    )
    flush_p.add_argument(
        "--trigger",
        default=FLUSH_TRIGGER_MANUAL,
        choices=sorted(ALLOWED_FLUSH_TRIGGERS),
        help="Flush trigger kind.",
    )

    ledger_p = nested.add_parser(
        OUTBOX_ACTION_LEDGER,
        parents=[json_parent],
        help="Search immutable EgressLedger audit receipts.",
    )
    ledger_p.add_argument("--sink", default=None, help="Optional Sink slug filter.")
    ledger_p.add_argument(
        "--outcome",
        default=None,
        choices=sorted(ALLOWED_EGRESS_OUTCOMES),
        help="Optional outcome filter.",
    )
    ledger_p.add_argument("--item-id", default=None, help="Optional OutboxItem ID filter.")


# endregion


# region CLI
def handle_outbox(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch `wt outbox {preview,list,approve,reject,flush,ledger}`."""
    action = str(args.outbox_action)
    handlers = {
        OUTBOX_ACTION_PREVIEW: _handle_outbox_preview,
        OUTBOX_ACTION_LIST: _handle_outbox_list,
        OUTBOX_ACTION_APPROVE: _handle_outbox_approve,
        OUTBOX_ACTION_REJECT: _handle_outbox_reject,
        OUTBOX_ACTION_FLUSH: _handle_outbox_flush,
        OUTBOX_ACTION_LEDGER: _handle_outbox_ledger,
    }
    return handlers[action](ctx, args)


def _handle_outbox_preview(ctx: CliContext, args: argparse.Namespace) -> int:
    """Compute dry-run rollup preview diffs and optionally stage proposals."""
    since = parse_cli_timestamp(args.since, field_name="since") if args.since else None
    until = parse_cli_timestamp(args.until, field_name="until") if args.until else None
    policy = RollupPolicy(
        grouping_window=str(args.grouping_window),
        min_duration_seconds=int(args.min_duration),
        rounding_quantum_seconds=int(args.rounding_quantum),
        rounding_mode=str(args.rounding_mode),
        redact_notes=not bool(args.include_notes),
        flush_cadence=str(args.cadence),
    )
    with ctx.open_ledger() as ledger, ctx.open_outbox_store() as store:
        engine = RollupEngine(ledger=ledger, outbox_store=store, telemetry=ctx.telemetry)
        preview = engine.preview_rollup(
            sink=str(args.sink),
            policy=policy,
            since=since,
            until=until,
        )
        staged: list[OutboxItem] = []
        if args.stage:
            actionable = [
                prop
                for prop, diff in zip(preview.proposals, preview.diffs, strict=True)
                if diff.change_kind != CHANGE_KIND_UNCHANGED
            ]
            staged = store.stage_proposals(actionable)

    if ctx.as_json:
        payload = preview.to_dict()
        payload["staged_items"] = [serialize_outbox_item(item) for item in staged]
        write_json(ctx.stdout, payload)
    else:
        write_line(ctx.stdout, preview.format_human())
        if args.stage:
            write_line(ctx.stdout, f"staged: {len(staged)} item(s)")
    return EXIT_SUCCESS


def _handle_outbox_list(ctx: CliContext, args: argparse.Namespace) -> int:
    """List `OutboxItems` filtered by status, sink, and cadence."""
    with ctx.open_outbox_store() as store:
        items = store.list_items(status=args.status, sink=args.sink, cadence=args.cadence)
    if ctx.as_json:
        write_json(ctx.stdout, {"items": [serialize_outbox_item(item) for item in items]})
    else:
        render_outbox_items_human(ctx.stdout, items)
    return EXIT_SUCCESS


def _handle_outbox_approve(ctx: CliContext, args: argparse.Namespace) -> int:
    """Approve an `OutboxItem` for egress."""
    with ctx.open_outbox_store() as store:
        item = store.approve_item(str(args.item_id))
    if ctx.as_json:
        write_json(ctx.stdout, {"item": serialize_outbox_item(item)})
    else:
        write_line(
            ctx.stdout,
            f"approved: {item.id} ({item.sink} {item.target_ref}, "
            f"{item.rounded_duration_seconds}s)",
        )
    return EXIT_SUCCESS


def _handle_outbox_reject(ctx: CliContext, args: argparse.Namespace) -> int:
    """Reject an `OutboxItem` with optional reason."""
    with ctx.open_outbox_store() as store:
        item = store.reject_item(str(args.item_id), reason=args.reason)
    if ctx.as_json:
        write_json(ctx.stdout, {"item": serialize_outbox_item(item)})
    else:
        write_line(ctx.stdout, f"rejected: {item.id} ({item.sink} {item.target_ref})")
    return EXIT_SUCCESS


def _handle_outbox_flush(ctx: CliContext, args: argparse.Namespace) -> int:
    """Flush due or explicit `approved` `OutboxItems` via `OutboxDispatcher`."""
    sinks = _resolve_cli_sinks(ctx)
    event_logger = StructuredEventLogger(telemetry_dir=ctx.settings.telemetry_dir)
    with ctx.open_outbox_store() as store:
        dispatcher = OutboxDispatcher(
            outbox_store=store,
            sinks=sinks,
            telemetry=ctx.telemetry,
            event_logger=event_logger,
            environ=ctx.environ,
        )
        if args.item_id is not None:
            results = [dispatcher.flush_item(str(args.item_id), trigger=str(args.trigger))]
        else:
            results = dispatcher.flush_due(
                trigger=str(args.trigger),
                sink=args.sink,
                cadence=args.cadence,
            )

    if ctx.as_json:
        write_json(ctx.stdout, {"results": [serialize_flush_result(res) for res in results]})
    else:
        render_flush_results_human(ctx.stdout, results)
    return EXIT_SUCCESS


def _handle_outbox_ledger(ctx: CliContext, args: argparse.Namespace) -> int:
    """Query immutable `EgressLedger` audit receipts."""
    with ctx.open_outbox_store() as store:
        records = store.list_egress_records(
            sink=args.sink,
            outcome=args.outcome,
            outbox_item_id=args.item_id,
        )
    if ctx.as_json:
        write_json(ctx.stdout, {"records": [serialize_egress_record(rec) for rec in records]})
    else:
        render_egress_records_human(ctx.stdout, records)
    return EXIT_SUCCESS


def _resolve_cli_sinks(ctx: CliContext) -> tuple[RollupSink, ...]:
    """Return active test sink override or build configured `JiraWorklogSink`."""
    override = _CLI_SINKS_OVERRIDE.get()
    if override is not None:
        return override
    try:
        client = JiraOAuthClient(
            config=JiraCloudConfig(),
            token_store=UserTokenStore(environ=ctx.environ),
            environ=ctx.environ,
        )
    except JiraConfigurationError:
        return ()
    return (JiraWorklogSink(client=client, telemetry=ctx.telemetry),)


# endregion
