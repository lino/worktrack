"""`wt` CLI subcommands for `clue`, `sensor`, and `reconcile`."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from worktrack.cli.context import (
    EXIT_SUCCESS,
    SUBCOMMAND_CLUE,
    SUBCOMMAND_RECONCILE,
    SUBCOMMAND_SENSOR,
    CliContext,
    parse_cli_timestamp,
    resolve_or_create_activity,
)
from worktrack.cli.render import write_json, write_line
from worktrack.collectors import (
    Clue,
    ClueValidationError,
    GitClueCollector,
    GitLogRunner,
    SubprocessGitLogRunner,
)
from worktrack.core import Ledger, LedgerValidationError
from worktrack.ipc.prompt import format_duration_compact
from worktrack.reconciler import (
    ALLOWED_RETROSPLIT_STRATEGIES,
    RETROSPLIT_STRATEGY_PROPORTIONAL,
    PlausibilityReconciler,
    ReconcileClueInput,
    ReconcilerValidationError,
    RetroSplitEngine,
    RetroSplitProposal,
)
from worktrack.sensors import (
    LocalWorkstationProbe,
    SensorValidationError,
    WorkplaceSensorEngine,
    WorkplaceSensorRule,
    WorkstationProbe,
)

# region Constants
ENV_SENSOR_RULES_JSON: Final = "WORKTRACK_SENSOR_RULES_JSON"
DEFAULT_CLUE_CONFIDENCE_FLOOR: Final = 0.01
HOURS_PER_DAY: Final = 24

_INJECTED_GIT_LOG_RUNNER: ContextVar[GitLogRunner | None] = ContextVar(
    "worktrack_cli_injected_git_log_runner", default=None
)
_INJECTED_WORKSTATION_PROBE: ContextVar[WorkstationProbe | None] = ContextVar(
    "worktrack_cli_injected_workstation_probe", default=None
)
# endregion


# region Resolution
@contextmanager
def use_git_log_runner(runner: GitLogRunner) -> Iterator[GitLogRunner]:
    """Bind a custom `GitLogRunner` for `wt clue git-sync` during tests."""
    token = _INJECTED_GIT_LOG_RUNNER.set(runner)
    try:
        yield runner
    finally:
        _INJECTED_GIT_LOG_RUNNER.reset(token)


@contextmanager
def use_workstation_probe(probe: WorkstationProbe) -> Iterator[WorkstationProbe]:
    """Bind a custom `WorkstationProbe` for `wt sensor evaluate` during tests."""
    token = _INJECTED_WORKSTATION_PROBE.set(probe)
    try:
        yield probe
    finally:
        _INJECTED_WORKSTATION_PROBE.reset(token)


def parse_sensor_rules_json(raw_json: str) -> tuple[WorkplaceSensorRule, ...]:
    """Parse a JSON array of `WorkplaceSensorRule` dictionaries."""
    cleaned = raw_json.strip()
    if not cleaned:
        return ()
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SensorValidationError(f"Invalid sensor rules JSON: {exc}") from exc
    if not isinstance(decoded, list):
        raise SensorValidationError("Sensor rules JSON must be a JSON array of rule objects.")
    rules: list[WorkplaceSensorRule] = []
    for item in decoded:
        if not isinstance(item, Mapping):
            raise SensorValidationError("Each sensor rule entry must be a JSON object.")
        rules.append(_build_sensor_rule_from_mapping(item))
    return tuple(rules)


def _build_sensor_rule_from_mapping(item: Mapping[str, Any]) -> WorkplaceSensorRule:
    """Construct a `WorkplaceSensorRule` from a decoded JSON mapping."""
    cidrs = (
        *_to_str_tuple(item.get("cidrs")),
        *_to_str_tuple(item.get("ipv4_cidrs")),
        *_to_str_tuple(item.get("ipv6_cidrs")),
    )
    disp, min_d, max_d, vpn = (
        item.get("display_count"),
        item.get("min_displays"),
        item.get("max_displays"),
        item.get("require_vpn"),
    )
    return WorkplaceSensorRule(
        rule_id=str(item.get("rule_id", "")),
        workplace=str(item.get("workplace", "")),
        cidrs=cidrs,
        wifi_ssids=_to_str_tuple(item.get("wifi_ssids")),
        vpn_interface_prefixes=_to_str_tuple(item.get("vpn_interface_prefixes")),
        display_count=int(disp) if disp is not None else None,
        min_displays=int(min_d) if min_d is not None else None,
        max_displays=int(max_d) if max_d is not None else None,
        require_vpn=bool(vpn) if vpn is not None else None,
        confidence=float(item.get("confidence", 0.90)),
        description=str(item.get("description", "")),
    )


def _to_str_tuple(raw: Any) -> tuple[str, ...]:
    """Coerce an optional sequence of strings into a `tuple[str, ...]`."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(str(v) for v in raw)


def clues_to_reconcile_inputs(clues: Sequence[Clue]) -> list[ReconcileClueInput]:
    """Convert stored `Clue` records with activity hints into `ReconcileClueInput` items."""
    return [
        ReconcileClueInput(
            activity_id=c.activity_id,
            external_ref=c.inferred_activity_key,
            timestamp=c.occurred_at,
            source=c.source,
            summary=c.summary,
            weight=max(float(c.confidence), DEFAULT_CLUE_CONFIDENCE_FLOOR),
            clue_id=c.id,
        )
        for c in clues
        if c.activity_id is not None or c.inferred_activity_key is not None
    ]


def _resolve_reconcile_window(
    raw_start: str | None, raw_end: str | None
) -> tuple[datetime, datetime]:
    """Resolve `--start` and `--end` CLI flags or default to today's UTC day window."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = parse_cli_timestamp(raw_start, field_name="--start") if raw_start else today_start
    end_dt = (
        parse_cli_timestamp(raw_end, field_name="--end")
        if raw_end
        else start_dt + timedelta(hours=HOURS_PER_DAY)
    )
    return start_dt, end_dt


def _build_retrosplit_proposal(
    ctx: CliContext, ledger: Ledger, args: argparse.Namespace
) -> RetroSplitProposal:
    """Resolve activities/weights and build a `RetroSplitProposal` for `--interval-id`."""
    interval = ledger.get_interval(str(args.interval_id))
    raw_activities: list[str] = list(args.activities or [])
    if not raw_activities:
        raise ReconcilerValidationError("At least one --activity must be specified.")
    resolved_ids = [
        resolve_or_create_activity(ledger, ident, create_if_missing=False).id
        for ident in raw_activities
    ]
    raw_weights: list[float] = list(args.weights or [])
    weights_map: dict[str, float] | None = None
    if raw_weights:
        if len(raw_weights) != len(resolved_ids):
            raise ReconcilerValidationError("--weights count must match --activity count.")
        weights_map = dict(zip(resolved_ids, raw_weights, strict=True))
    with ctx.open_planner(ledger=ledger) as planner, ctx.open_clue_store() as clue_store:
        clues = clue_store.list_clues(since=interval.started_at, until=interval.ended_at)
        engine = RetroSplitEngine(ledger, agenda_planner=planner, telemetry=ctx.telemetry)
        return engine.propose_split(
            interval,
            strategy=str(args.strategy),
            activity_ids=resolved_ids,
            clues=clues_to_reconcile_inputs(clues),
            weights=weights_map,
        )


# endregion


# region Subcommands
def register_clues_sensors_subparsers(
    subparsers: argparse._SubParsersAction[Any], json_parent: argparse.ArgumentParser
) -> None:
    """Register `wt clue`, `wt sensor`, and `wt reconcile` subparsers."""
    _register_clue_subparser(subparsers, json_parent)
    _register_sensor_subparser(subparsers, json_parent)
    _register_reconcile_subparser(subparsers, json_parent)


def _register_clue_subparser(
    subparsers: argparse._SubParsersAction[Any], json_parent: argparse.ArgumentParser
) -> None:
    """Register `wt clue {list,git-sync}` subcommands."""
    p_clue = subparsers.add_parser(SUBCOMMAND_CLUE, parents=[json_parent], help="Manage Clues.")
    p_clue.set_defaults(handler=handle_clue, canonical_subcommand=SUBCOMMAND_CLUE)
    clue_sub = p_clue.add_subparsers(dest="clue_action", parser_class=type(p_clue))

    p_list = clue_sub.add_parser("list", parents=[json_parent], help="List stored Clues.")
    p_list.add_argument("--source", default=None, help="Filter by clue source slug.")
    p_list.add_argument("--start", default=None, help="Inclusive window start ISO-8601.")
    p_list.add_argument("--end", default=None, help="Exclusive window end ISO-8601.")
    p_list.set_defaults(handler=handle_clue, canonical_subcommand=SUBCOMMAND_CLUE)

    p_sync = clue_sub.add_parser("git-sync", parents=[json_parent], help="Sync Git commit clues.")
    p_sync.add_argument("--repo", default=None, help="Path to local Git repository.")
    p_sync.add_argument("--since", default=None, help="Inclusive lower bound ISO-8601.")
    p_sync.add_argument("--until", default=None, help="Inclusive upper bound ISO-8601.")
    p_sync.add_argument("--author", default=None, help="Author name/email filter substring.")
    p_sync.set_defaults(handler=handle_clue, canonical_subcommand=SUBCOMMAND_CLUE)


def _register_sensor_subparser(
    subparsers: argparse._SubParsersAction[Any], json_parent: argparse.ArgumentParser
) -> None:
    """Register `wt sensor evaluate` subcommand."""
    p_s = subparsers.add_parser(SUBCOMMAND_SENSOR, parents=[json_parent], help="Evaluate sensors.")
    p_s.set_defaults(handler=handle_sensor, canonical_subcommand=SUBCOMMAND_SENSOR)
    sensor_sub = p_s.add_subparsers(dest="sensor_action", parser_class=type(p_s))
    p_eval = sensor_sub.add_parser("evaluate", parents=[json_parent], help="Probe workstation.")
    p_eval.add_argument("--date", default=None, help="Scope date (YYYY-MM-DD).")
    p_eval.add_argument("--apply", action="store_true", help="Assert workplace ContextFact.")
    p_eval.add_argument("--rules-json", default=None, help="Inline JSON array of rules.")
    p_eval.set_defaults(handler=handle_sensor, canonical_subcommand=SUBCOMMAND_SENSOR)


def _register_reconcile_subparser(
    subparsers: argparse._SubParsersAction[Any], json_parent: argparse.ArgumentParser
) -> None:
    """Register `wt reconcile {inspect,propose,apply}` subcommands."""
    p_r = subparsers.add_parser(SUBCOMMAND_RECONCILE, parents=[json_parent], help="Reconciler.")
    p_r.set_defaults(handler=handle_reconcile, canonical_subcommand=SUBCOMMAND_RECONCILE)
    rec_sub = p_r.add_subparsers(dest="reconcile_action", parser_class=type(p_r))
    p_insp = rec_sub.add_parser("inspect", parents=[json_parent], help="Inspect window gaps.")
    p_insp.add_argument("--start", default=None, help="Window start ISO-8601.")
    p_insp.add_argument("--end", default=None, help="Window end ISO-8601.")
    p_insp.set_defaults(handler=handle_reconcile, canonical_subcommand=SUBCOMMAND_RECONCILE)
    choices = sorted(ALLOWED_RETROSPLIT_STRATEGIES)
    for action_name in ("propose", "apply"):
        p_act = rec_sub.add_parser(
            action_name, parents=[json_parent], help=f"RetroSplit {action_name}."
        )
        p_act.add_argument("--interval-id", required=True, help="Target chaotic Interval ID.")
        p_act.add_argument("--activity", dest="activities", action="append", required=True)
        p_act.add_argument("--strategy", default=RETROSPLIT_STRATEGY_PROPORTIONAL, choices=choices)
        p_act.add_argument("--weights", type=float, action="append", default=None)
        p_act.set_defaults(handler=handle_reconcile, canonical_subcommand=SUBCOMMAND_RECONCILE)


def handle_clue(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch `wt clue {list,git-sync}` subcommands."""
    action = getattr(args, "clue_action", None) or "list"
    if action == "list":
        return _handle_clue_list(ctx, args)
    if action == "git-sync":
        return _handle_clue_git_sync(ctx, args)
    raise ClueValidationError(f"Unknown clue action '{action}'.")


def _handle_clue_list(ctx: CliContext, args: argparse.Namespace) -> int:
    """List stored `Clue` records filtered by `--source`, `--start`, and `--end`."""
    start_dt = parse_cli_timestamp(args.start, field_name="--start") if args.start else None
    end_dt = parse_cli_timestamp(args.end, field_name="--end") if args.end else None
    with ctx.open_clue_store() as store:
        clues = store.list_clues(source=args.source, since=start_dt, until=end_dt)
    if ctx.as_json:
        write_json(ctx.stdout, {"clues": [c.to_dict() for c in clues]})
        return EXIT_SUCCESS
    if not clues:
        write_line(ctx.stdout, "No clues recorded.")
        return EXIT_SUCCESS
    for clue in clues:
        key_lbl = f" [{clue.inferred_activity_key}]" if clue.inferred_activity_key else ""
        write_line(ctx.stdout, f"{clue.id}  {clue.source}{key_lbl}  {clue.summary}")
    return EXIT_SUCCESS


def _handle_clue_git_sync(ctx: CliContext, args: argparse.Namespace) -> int:
    """Run `GitClueCollector.sync` against `--repo` into `ClueStore` and `Ledger`."""
    repo_path = Path(args.repo).expanduser() if args.repo else Path.cwd()
    since_dt = parse_cli_timestamp(args.since, field_name="--since") if args.since else None
    until_dt = parse_cli_timestamp(args.until, field_name="--until") if args.until else None
    runner = _INJECTED_GIT_LOG_RUNNER.get() or SubprocessGitLogRunner()
    with ctx.open_ledger() as ledger, ctx.open_clue_store() as clue_store:
        collector = GitClueCollector(
            clue_store=clue_store, git_runner=runner, ledger=ledger, telemetry=ctx.telemetry
        )
        summary = collector.sync(
            repo_paths=(repo_path,),
            author_pattern=args.author,
            since=since_dt,
            until=until_dt,
            ledger=ledger,
        )
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "commits_scanned": summary.commits_scanned,
                "forge_records_scanned": summary.forge_records_scanned,
                "clues_upserted": summary.clues_upserted,
                "activities_linked": summary.activities_linked,
                "clues": [c.to_dict() for c in summary.clues],
            },
        )
        return EXIT_SUCCESS
    upserted, linked = summary.clues_upserted, summary.activities_linked
    write_line(ctx.stdout, f"Git clue sync complete: {upserted} clue(s), {linked} linked.")
    return EXIT_SUCCESS


def handle_sensor(ctx: CliContext, args: argparse.Namespace) -> int:
    """Evaluate `WorkplaceSensorEngine` and optionally assert `workplace` `ContextFact`."""
    action = getattr(args, "sensor_action", None) or "evaluate"
    if action != "evaluate":
        raise SensorValidationError(f"Unknown sensor action '{action}'.")
    raw_rules = (
        args.rules_json
        if args.rules_json is not None
        else ctx.environ.get(ENV_SENSOR_RULES_JSON, "")
    )
    rules = parse_sensor_rules_json(raw_rules)
    probe = _INJECTED_WORKSTATION_PROBE.get() or LocalWorkstationProbe()
    with ctx.open_ledger() as ledger, ctx.open_clue_store() as clue_store:
        engine = WorkplaceSensorEngine(
            rules=rules, probe=probe, clue_store=clue_store, ledger=ledger
        )
        suggestion = engine.evaluate(scope_date=args.date, assert_fact=bool(args.apply))
    if suggestion is None:
        if ctx.as_json:
            write_json(ctx.stdout, {"matched": False, "workplace": None})
        else:
            write_line(ctx.stdout, "No workplace sensor rule matched.")
        return EXIT_SUCCESS
    if ctx.as_json:
        write_json(ctx.stdout, {"matched": True, **suggestion.to_dict()})
        return EXIT_SUCCESS
    write_line(
        ctx.stdout,
        f"Workplace sensor matched '{suggestion.workplace}' (rule={suggestion.rule_id}).",
    )
    return EXIT_SUCCESS


def handle_reconcile(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch `wt reconcile {inspect,propose,apply}` subcommands."""
    action = getattr(args, "reconcile_action", None)
    if action == "inspect":
        return _handle_reconcile_inspect(ctx, args)
    if action == "propose":
        return _handle_reconcile_propose(ctx, args)
    if action == "apply":
        return _handle_reconcile_apply(ctx, args)
    raise LedgerValidationError("Specify a reconcile action: inspect, propose, or apply.")


def _handle_reconcile_inspect(ctx: CliContext, args: argparse.Namespace) -> int:
    """Inspect plausibility gaps, unallocated intervals, and candidate clues."""
    start_dt, end_dt = _resolve_reconcile_window(args.start, args.end)
    with (
        ctx.open_ledger() as ledger,
        ctx.open_planner(ledger=ledger) as planner,
        ctx.open_clue_store() as clue_store,
    ):
        clues = clue_store.list_clues(since=start_dt, until=end_dt)
        reconciler = PlausibilityReconciler(ledger, agenda_planner=planner)
        report = reconciler.inspect(start_dt, end_dt, clues=clues_to_reconcile_inputs(clues))
    rep = report.to_dict()
    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                **rep,
                "unbooked_activities": rep["gaps"],
                "unallocated_intervals": rep["candidate_intervals"],
                "candidate_clues": [c.to_dict() for c in clues],
            },
        )
        return EXIT_SUCCESS
    dur = format_duration_compact(report.total_unallocated_seconds)
    write_line(
        ctx.stdout,
        f"Reconcile inspect: {len(report.gaps)} gap(s), "
        f"{len(report.candidate_intervals)} unallocated interval(s) ({dur}).",
    )
    return EXIT_SUCCESS


def _handle_reconcile_propose(ctx: CliContext, args: argparse.Namespace) -> int:
    """Generate and display a `RetroSplitProposal` without mutating the `Ledger`."""
    with ctx.open_ledger() as ledger:
        proposal = _build_retrosplit_proposal(ctx, ledger, args)
    if ctx.as_json:
        write_json(ctx.stdout, {"proposal": proposal.to_dict()})
        return EXIT_SUCCESS
    for slc in proposal.slices:
        dur = format_duration_compact(slc.duration_seconds)
        write_line(ctx.stdout, f"  {slc.activity_name} ({slc.activity_id}): {dur}")
    return EXIT_SUCCESS


def _handle_reconcile_apply(ctx: CliContext, args: argparse.Namespace) -> int:
    """Generate and atomically apply a `RetroSplitProposal` to the `Ledger`."""
    with ctx.open_ledger() as ledger, ctx.open_planner(ledger=ledger) as planner:
        proposal = _build_retrosplit_proposal(ctx, ledger, args)
        engine = RetroSplitEngine(ledger, agenda_planner=planner, telemetry=ctx.telemetry)
        result = engine.apply_proposal(proposal)
    res_dict = result.to_dict()
    if ctx.as_json:
        write_json(ctx.stdout, {**res_dict, "created_intervals": res_dict["intervals"]})
        return EXIT_SUCCESS
    write_line(
        ctx.stdout,
        f"Applied RetroSplit ({result.strategy}) across {len(result.intervals)} interval(s).",
    )
    return EXIT_SUCCESS


# endregion
