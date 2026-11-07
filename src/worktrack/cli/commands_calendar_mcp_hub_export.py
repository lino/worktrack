"""`wt` CLI subcommands for `calendar`, `mcp`, `hub`, and `export`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TextIO

from worktrack.cli.context import (
    EXIT_SUCCESS,
    SUBCOMMAND_CALENDAR,
    SUBCOMMAND_EXPORT,
    SUBCOMMAND_HUB,
    SUBCOMMAND_MCP,
    CliContext,
    parse_cli_timestamp,
)
from worktrack.cli.render import serialize_outbox_item, write_json, write_line
from worktrack.collectors import apply_clue_migrations
from worktrack.connectors.calendar import (
    DEFAULT_CALENDAR_TARGET_REF,
    DEFAULT_STANDING_MEETINGS_NAME,
    CalendarBucketMapping,
    CalendarConfigurationError,
    CalendarFocusBlockStaging,
    IcalCalendarSource,
)
from worktrack.exporters import (
    ALLOWED_EXPORT_FORMATS,
    EXPORT_FORMAT_MARKDOWN,
    ExporterValidationError,
    TaxAndAttributionExporter,
)
from worktrack.hub_sync import (
    EntityCatalogSyncClient,
    HubConfigurationError,
    HubEndpointConfig,
    HubSyncTransport,
    UrllibHubSyncTransport,
)
from worktrack.mcp import WorktrackMcpServer
from worktrack.outbox import RollupEngine

# region Constants
HOURS_PER_DAY: Final = 24

_INJECTED_CLI_STDIN: ContextVar[TextIO | None] = ContextVar(
    "worktrack_cli_injected_stdin",
    default=None,
)
_INJECTED_HUB_TRANSPORT: ContextVar[HubSyncTransport | None] = ContextVar(
    "worktrack_cli_injected_hub_transport",
    default=None,
)
# endregion


# region Resolution
@contextmanager
def use_cli_stdin(stream: TextIO) -> Iterator[TextIO]:
    """Bind an in-memory `stdin` stream for `wt mcp` tests."""
    token = _INJECTED_CLI_STDIN.set(stream)
    try:
        yield stream
    finally:
        _INJECTED_CLI_STDIN.reset(token)


@contextmanager
def use_hub_transport(transport: HubSyncTransport) -> Iterator[HubSyncTransport]:
    """Bind an injectable `HubSyncTransport` for `wt hub pull-catalog` tests."""
    token = _INJECTED_HUB_TRANSPORT.set(transport)
    try:
        yield transport
    finally:
        _INJECTED_HUB_TRANSPORT.reset(token)


def _resolve_day_window(
    raw_start: str | None,
    raw_end: str | None,
) -> tuple[datetime, datetime]:
    """Resolve optional `--start` and `--end` flags or default to today's UTC window."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = parse_cli_timestamp(raw_start, field_name="--start") if raw_start else today_start
    end_dt = (
        parse_cli_timestamp(raw_end, field_name="--end")
        if raw_end
        else start_dt + timedelta(hours=HOURS_PER_DAY)
    )
    return start_dt, end_dt


# endregion


# region Subcommands
def register_calendar_mcp_hub_export_subparsers(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `wt calendar`, `wt mcp`, `wt hub`, and `wt export` subparsers."""
    _register_calendar_subparser(subparsers, json_parent)
    _register_mcp_subparser(subparsers, json_parent)
    _register_hub_subparser(subparsers, json_parent)
    _register_export_subparser(subparsers, json_parent)


def _register_calendar_subparser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `wt calendar {sync,stage-focus}` subcommands."""
    p_cal = subparsers.add_parser(
        SUBCOMMAND_CALENDAR, parents=[json_parent], help="Sync iCal or stage FocusBlocks."
    )
    p_cal.set_defaults(handler=handle_calendar, canonical_subcommand=SUBCOMMAND_CALENDAR)
    cal_sub = p_cal.add_subparsers(dest="calendar_action", parser_class=type(p_cal))

    p_sync = cal_sub.add_parser("sync", parents=[json_parent], help="Import .ics calendar file.")
    p_sync.add_argument("--ics-file", required=True, help="Path to RFC 5545 .ics file.")
    p_sync.add_argument(
        "--standing-activity",
        default=DEFAULT_STANDING_MEETINGS_NAME,
        help="Standing Activity name for unmatched meetings.",
    )
    p_sync.add_argument("--target-ref", default=DEFAULT_CALENDAR_TARGET_REF, help="Calendar ID.")
    p_sync.set_defaults(handler=handle_calendar, canonical_subcommand=SUBCOMMAND_CALENDAR)

    p_stage = cal_sub.add_parser("stage-focus", parents=[json_parent], help="Stage FocusBlocks.")
    p_stage.add_argument("--start", default=None, help="Window start ISO-8601.")
    p_stage.add_argument("--end", default=None, help="Window end ISO-8601.")
    p_stage.add_argument("--target-ref", default=DEFAULT_CALENDAR_TARGET_REF, help="Calendar ID.")
    p_stage.set_defaults(handler=handle_calendar, canonical_subcommand=SUBCOMMAND_CALENDAR)


def _register_mcp_subparser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `wt mcp [--once]` subcommand."""
    p_mcp = subparsers.add_parser(
        SUBCOMMAND_MCP, parents=[json_parent], help="Run MCP server over stdio."
    )
    p_mcp.add_argument("--once", action="store_true", help="Process a single JSON-RPC line.")
    p_mcp.set_defaults(handler=handle_mcp, canonical_subcommand=SUBCOMMAND_MCP)


def _register_hub_subparser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `wt hub {status,pull-catalog}` subcommands."""
    p_hub = subparsers.add_parser(
        SUBCOMMAND_HUB, parents=[json_parent], help="Inspect Hub rollups or pull Entity catalog."
    )
    p_hub.set_defaults(handler=handle_hub, canonical_subcommand=SUBCOMMAND_HUB)
    hub_sub = p_hub.add_subparsers(dest="hub_action", parser_class=type(p_hub))

    p_stat = hub_sub.add_parser("status", parents=[json_parent], help="Inspect Hub status.")
    p_stat.set_defaults(handler=handle_hub, canonical_subcommand=SUBCOMMAND_HUB)

    p_pull = hub_sub.add_parser("pull-catalog", parents=[json_parent], help="Pull Entity catalog.")
    p_pull.add_argument("--hub-url", default=None, help="Override Hub base URL.")
    p_pull.set_defaults(handler=handle_hub, canonical_subcommand=SUBCOMMAND_HUB)


def _register_export_subparser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `wt export {tax,attribution}` subcommands."""
    p_exp = subparsers.add_parser(
        SUBCOMMAND_EXPORT, parents=[json_parent], help="Export tax and Attribution reports."
    )
    p_exp.set_defaults(handler=handle_export, canonical_subcommand=SUBCOMMAND_EXPORT)
    exp_sub = p_exp.add_subparsers(dest="export_action", parser_class=type(p_exp))

    p_tax = exp_sub.add_parser("tax", parents=[json_parent], help="Export workplace tax report.")
    p_tax.add_argument("--year", type=int, required=True, help="Four-digit calendar year.")
    p_tax.add_argument(
        "--format",
        dest="export_format",
        default=EXPORT_FORMAT_MARKDOWN,
        choices=sorted(ALLOWED_EXPORT_FORMATS),
        help="Output format (csv, json, or markdown).",
    )
    p_tax.set_defaults(handler=handle_export, canonical_subcommand=SUBCOMMAND_EXPORT)

    p_attr = exp_sub.add_parser("attribution", parents=[json_parent], help="Export Attribution.")
    p_attr.add_argument("--start", required=True, help="Window start ISO-8601.")
    p_attr.add_argument("--end", required=True, help="Window end ISO-8601.")
    p_attr.add_argument("--kind", default=None, help="Optional target Entity kind filter.")
    p_attr.add_argument(
        "--format",
        dest="export_format",
        default=EXPORT_FORMAT_MARKDOWN,
        choices=sorted(ALLOWED_EXPORT_FORMATS),
        help="Output format (csv, json, or markdown).",
    )
    p_attr.set_defaults(handler=handle_export, canonical_subcommand=SUBCOMMAND_EXPORT)


def handle_calendar(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch `wt calendar {sync,stage-focus}` subcommands."""
    action = getattr(args, "calendar_action", None)
    if action == "sync":
        return _handle_calendar_sync(ctx, args)
    if action == "stage-focus":
        return _handle_calendar_stage_focus(ctx, args)
    raise CalendarConfigurationError("Specify a calendar action: sync or stage-focus.")


def _handle_calendar_sync(ctx: CliContext, args: argparse.Namespace) -> int:
    """Import `.ics` calendar events via `IcalCalendarSource` into `Ledger`."""
    ics_path = Path(str(args.ics_file)).expanduser()
    mapping = CalendarBucketMapping(
        default_standing_activity_name=str(args.standing_activity or DEFAULT_STANDING_MEETINGS_NAME)
    )
    source = IcalCalendarSource(
        bucket_mapping=mapping,
        calendar_id=str(args.target_ref or DEFAULT_CALENDAR_TARGET_REF),
        telemetry=ctx.telemetry,
    )
    with ctx.open_ledger() as ledger:
        result = source.sync(ledger, ics_path=ics_path)
    payload: dict[str, object] = {**result.to_dict(), "events_synced": result.events_fetched}
    if ctx.as_json:
        write_json(ctx.stdout, payload)
    else:
        write_line(
            ctx.stdout,
            f"Calendar sync complete: {result.events_fetched} event(s), "
            f"{result.intervals_created} interval(s) created.",
        )
    return EXIT_SUCCESS


def _handle_calendar_stage_focus(ctx: CliContext, args: argparse.Namespace) -> int:
    """Stage `expose_busy=True` `FocusBlock` items into `OutboxStore`."""
    start_dt, end_dt = _resolve_day_window(args.start, args.end)
    target_ref = str(args.target_ref or DEFAULT_CALENDAR_TARGET_REF)
    with ctx.open_planner() as planner, ctx.open_outbox_store() as store:
        blocks = planner.list_focus_blocks(since=start_dt, until=end_dt)
        staging = CalendarFocusBlockStaging(outbox_store=store, default_target_ref=target_ref)
        staged = staging.stage_focus_blocks(blocks, target_ref=target_ref)
    serialized_items: list[dict[str, object]] = []
    for item in staged:
        base = serialize_outbox_item(item)
        prop_payload = dict(item.payload_preview)
        if "summary" in prop_payload and "busy_summary" not in prop_payload:
            prop_payload["busy_summary"] = prop_payload["summary"]
        serialized_items.append({**base, "payload": prop_payload})
    if ctx.as_json:
        write_json(ctx.stdout, {"staged_items": serialized_items})
    else:
        write_line(ctx.stdout, f"Staged {len(staged)} focus block(s) into Outbox.")
    return EXIT_SUCCESS


def handle_mcp(ctx: CliContext, args: argparse.Namespace) -> int:
    """Run `WorktrackMcpServer` over `stdin`/`stdout` (single-line when `--once` is set)."""
    in_stream = _INJECTED_CLI_STDIN.get() or sys.stdin
    with (
        ctx.open_ledger() as ledger,
        ctx.open_planner(ledger=ledger) as planner,
        ctx.open_outbox_store() as outbox_store,
    ):
        apply_clue_migrations(ledger._require_conn())
        rollup_engine = RollupEngine(
            ledger=ledger, outbox_store=outbox_store, telemetry=ctx.telemetry
        )
        server = WorktrackMcpServer(
            ledger,
            outbox_store=outbox_store,
            agenda_planner=planner,
            rollup_engine=rollup_engine,
            telemetry=ctx.telemetry,
        )
        if bool(getattr(args, "once", False)):
            raw_line = in_stream.readline()
            if raw_line.strip():
                response_line = server.handle_json_line(raw_line)
                if response_line is not None:
                    ctx.stdout.write(f"{response_line}\n")
                    ctx.stdout.flush()
            return EXIT_SUCCESS
        server.serve_stdio(in_stream, ctx.stdout)
    return EXIT_SUCCESS


def handle_hub(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch `wt hub {status,pull-catalog}` subcommands."""
    action = getattr(args, "hub_action", None) or "status"
    if action == "status":
        return _handle_hub_status(ctx)
    if action == "pull-catalog":
        return _handle_hub_pull_catalog(ctx, args)
    raise HubConfigurationError(f"Unknown hub action '{action}'.")


def _handle_hub_status(ctx: CliContext) -> int:
    """Inspect ingested rollups and shared catalog in `HubRollupIngestStore`."""
    with ctx.open_hub_ingest_store() as store:
        rollups = store.list_ingested_rollups()
        snapshot = store.get_catalog_snapshot()
    payload: dict[str, object] = {
        "ingested_rollups_count": len(rollups),
        "catalog_entities_count": len(snapshot.entities),
        "attribution_templates_count": len(snapshot.attribution_templates),
        "rollups": [r.to_dict() for r in rollups],
        "catalog": snapshot.to_dict(),
    }
    if ctx.as_json:
        write_json(ctx.stdout, payload)
    else:
        write_line(
            ctx.stdout,
            f"Hub status: {len(rollups)} rollup(s), {len(snapshot.entities)} catalog entit(ies).",
        )
    return EXIT_SUCCESS


def _handle_hub_pull_catalog(ctx: CliContext, args: argparse.Namespace) -> int:
    """Pull shared `Entity` catalog and default `Attribution` templates into local `Ledger`."""
    transport = _INJECTED_HUB_TRANSPORT.get() or UrllibHubSyncTransport()
    hub_cfg = HubEndpointConfig(base_url=str(args.hub_url) if args.hub_url else None)
    with ctx.open_ledger() as ledger:
        client = EntityCatalogSyncClient(
            ledger=ledger,
            transport=transport,
            config=hub_cfg,
            telemetry=ctx.telemetry,
            environ=ctx.environ,
        )
        result = client.sync_catalog()
    if ctx.as_json:
        write_json(ctx.stdout, result.to_dict())
    else:
        write_line(
            ctx.stdout,
            f"Hub catalog sync complete: {result.entities_created} created, "
            f"{result.entities_updated} updated.",
        )
    return EXIT_SUCCESS


def handle_export(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch `wt export {tax,attribution}` subcommands."""
    action = getattr(args, "export_action", None)
    if action == "tax":
        return _handle_export_tax(ctx, args)
    if action == "attribution":
        return _handle_export_attribution(ctx, args)
    raise ExporterValidationError("Specify an export action: tax or attribution.")


def _handle_export_tax(ctx: CliContext, args: argparse.Namespace) -> int:
    """Generate and render a `WorkplaceTaxReport` for `--year`."""
    fmt = str(args.export_format)
    with ctx.open_ledger() as ledger:
        exporter = TaxAndAttributionExporter(ledger, telemetry=ctx.telemetry)
        report = exporter.build_workplace_tax_report(year=int(args.year))
        rendered = exporter.export_workplace_tax_report(year=int(args.year), fmt=fmt)
    if ctx.as_json:
        report_dict: dict[str, object] = {
            **report.to_dict(),
            "workplace_days": dict(report.days_by_workplace),
        }
        write_json(ctx.stdout, {"format": fmt, "report": report_dict, "rendered": rendered})
    else:
        ctx.stdout.write(rendered if rendered.endswith("\n") else f"{rendered}\n")
    return EXIT_SUCCESS


def _handle_export_attribution(ctx: CliContext, args: argparse.Namespace) -> int:
    """Generate and render an `EntityAttributionReport` over `[--start, --end)`."""
    fmt = str(args.export_format)
    since_dt = parse_cli_timestamp(str(args.start), field_name="--start")
    until_dt = parse_cli_timestamp(str(args.end), field_name="--end")
    with ctx.open_ledger() as ledger:
        exporter = TaxAndAttributionExporter(ledger, telemetry=ctx.telemetry)
        report = exporter.build_entity_attribution_report(
            since=since_dt, until=until_dt, target_kind=args.kind
        )
        rendered = exporter.export_entity_attribution_report(
            since=since_dt, until=until_dt, target_kind=args.kind, fmt=fmt
        )
    if ctx.as_json:
        raw_dict = report.to_dict()
        enriched_rows = [
            {**row.to_dict(), "entity_key": row.key, "entity_kind": row.kind} for row in report.rows
        ]
        write_json(
            ctx.stdout,
            {
                "format": fmt,
                "report": {**raw_dict, "rows": enriched_rows},
                "rendered": rendered,
            },
        )
    else:
        ctx.stdout.write(rendered if rendered.endswith("\n") else f"{rendered}\n")
    return EXIT_SUCCESS


# endregion
