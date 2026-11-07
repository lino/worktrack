"""Modular `wt` argument parser builder and top-level CLI dispatcher."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from typing import Any, NoReturn, Protocol, TextIO

from worktrack.agenda.models import AgendaError
from worktrack.cli.commands_calendar_mcp_hub_export import (
    register_calendar_mcp_hub_export_subparsers,
)
from worktrack.cli.commands_capture import register_capture_subparsers
from worktrack.cli.commands_clues_sensors import register_clues_sensors_subparsers
from worktrack.cli.commands_ops import register_ops_subparsers
from worktrack.cli.commands_outbox import register_outbox_subparsers
from worktrack.cli.commands_planning import register_planning_subparsers
from worktrack.cli.commands_web import register_web_subparsers
from worktrack.cli.context import (
    EXIT_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    OUTCOME_ERROR,
    OUTCOME_OK,
    SUBCOMMAND_HELP,
    SUBCOMMAND_STATUS,
    CliContext,
    detect_subcommand_from_argv,
    normalize_cli_subcommand,
)
from worktrack.cli.render import write_json, write_line
from worktrack.collectors import CollectorError
from worktrack.config import ConfigurationError, WorktrackSettings
from worktrack.connectors.calendar import CalendarConnectorError
from worktrack.connectors.exceptions import ConnectorError
from worktrack.core.exceptions import LedgerError
from worktrack.exporters import ExporterError
from worktrack.hub_sync import HubSyncError
from worktrack.ipc.models import IpcError
from worktrack.mcp import McpError
from worktrack.outbox.exceptions import OutboxError
from worktrack.reconciler import ReconcilerError
from worktrack.secrets.exceptions import SecretError, scrub_secrets
from worktrack.sensors import SensorError
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
CLI_PROG_NAME = "wt"
PORCELAIN_FLAG = "--porcelain"

type SubparserRegistrar = Callable[
    [argparse._SubParsersAction[Any], argparse.ArgumentParser],
    None,
]

DEFAULT_SUBPARSER_REGISTRARS: tuple[SubparserRegistrar, ...] = (
    register_capture_subparsers,
    register_planning_subparsers,
    register_outbox_subparsers,
    register_web_subparsers,
    register_clues_sensors_subparsers,
    register_calendar_mcp_hub_export_subparsers,
    register_ops_subparsers,
)

_ACTIVE_IO_STREAMS: ContextVar[tuple[TextIO, TextIO] | None] = ContextVar(
    "worktrack_cli_active_io_streams",
    default=None,
)
# endregion


# region Exceptions
class _CliHelpSignalError(Exception):
    """Internal control-flow signal raised after rendering `--help` output."""


class _CliUsageError(Exception):
    """Internal control-flow signal raised on CLI argument parsing errors."""


# endregion


# region CLI
class _SupportsWriteStr(Protocol):
    """Structural protocol matching streams accepting string writes."""

    def write(self, s: str, /) -> object:
        """Write text to the underlying stream."""


class StreamArgumentParser(argparse.ArgumentParser):
    """`ArgumentParser` routing messages to injectable streams without `SystemExit`."""

    def _print_message(
        self,
        message: str,
        file: _SupportsWriteStr | None = None,
    ) -> None:
        """Write help or usage message to the active injected `stdout`/`stderr` stream."""
        if not message:
            return
        streams = _ACTIVE_IO_STREAMS.get()
        if streams is None:
            target: _SupportsWriteStr = sys.stdout if file is None else file
        else:
            out_stream, err_stream = streams
            target = err_stream if file is sys.stderr else out_stream
        target.write(message)

    def exit(self, status: int = EXIT_SUCCESS, message: str | None = None) -> NoReturn:
        """Intercept `argparse` exit calls and raise typed internal signals."""
        if message:
            self._print_message(message, sys.stderr)
        if status == EXIT_SUCCESS:
            raise _CliHelpSignalError
        raise _CliUsageError

    def error(self, message: str) -> NoReturn:
        """Format usage error onto injected `stderr` and raise `_CliUsageError`."""
        streams = _ACTIVE_IO_STREAMS.get()
        err_stream: _SupportsWriteStr = sys.stderr if streams is None else streams[1]
        self.print_usage(err_stream)
        self._print_message(f"{self.prog}: error: {message}\n", sys.stderr)
        raise _CliUsageError


def build_json_parent_parser() -> argparse.ArgumentParser:
    """Create shared parent parser enabling `--json` on any `wt` subcommand."""
    parent = StreamArgumentParser(add_help=False)
    parent.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit structured JSON output.",
    )
    return parent


def build_argument_parser(
    *,
    registrars: Sequence[SubparserRegistrar] = DEFAULT_SUBPARSER_REGISTRARS,
) -> StreamArgumentParser:
    """Construct the modular `wt` CLI `ArgumentParser` across registered command groups."""
    json_parent = build_json_parent_parser()
    parser = StreamArgumentParser(
        prog=CLI_PROG_NAME,
        description="Worktrack (wt) — anti-surveillance personal time ledger and horizon planner.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=False,
        help="Emit structured JSON output.",
    )
    subparsers = parser.add_subparsers(
        dest="subcommand",
        parser_class=StreamArgumentParser,
        metavar="<subcommand>",
    )
    for registrar in registrars:
        registrar(subparsers, json_parent)
    return parser


def parse_cli_args(
    parser: StreamArgumentParser,
    argv: Sequence[str],
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> argparse.Namespace:
    """Parse `argv` while binding `stdout` and `stderr` for help/error messages."""
    token = _ACTIVE_IO_STREAMS.set((stdout, stderr))
    try:
        return parser.parse_args(list(argv))
    finally:
        _ACTIVE_IO_STREAMS.reset(token)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    registrars: Sequence[SubparserRegistrar] = DEFAULT_SUBPARSER_REGISTRARS,
) -> int:
    """Execute `wt` CLI command and return process exit status (`0` on success)."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    out_stream = sys.stdout if stdout is None else stdout
    err_stream = sys.stderr if stderr is None else stderr
    env = os.environ if environ is None else environ
    fallback_sub = detect_subcommand_from_argv(raw_argv)

    try:
        settings = WorktrackSettings.from_env(environ=env)
        recorder = TelemetryRecorder.from_settings(settings)
    except ConfigurationError as exc:
        return _handle_config_error(exc, fallback_sub, raw_argv, err_stream)

    parser = build_argument_parser(registrars=registrars)
    if not raw_argv:
        return _render_root_help(parser, recorder, out_stream, err_stream)

    try:
        args = parse_cli_args(parser, raw_argv, stdout=out_stream, stderr=err_stream)
    except _CliHelpSignalError:
        recorder.record_cli_command(subcommand=SUBCOMMAND_HELP, outcome=OUTCOME_OK)
        return EXIT_SUCCESS
    except _CliUsageError:
        recorder.record_cli_command(subcommand=fallback_sub, outcome=OUTCOME_ERROR)
        return EXIT_USAGE_ERROR

    return _execute_parsed_command(args, settings, recorder, env, out_stream, err_stream)


def _handle_config_error(
    exc: ConfigurationError,
    fallback_sub: str,
    raw_argv: Sequence[str],
    err_stream: TextIO,
) -> int:
    """Handle bootstrap `ConfigurationError` while preserving graceful `--porcelain`."""
    if fallback_sub == SUBCOMMAND_STATUS and PORCELAIN_FLAG in raw_argv:
        return EXIT_SUCCESS
    write_line(err_stream, f"error: {exc}")
    return EXIT_USAGE_ERROR


def _render_root_help(
    parser: StreamArgumentParser,
    recorder: TelemetryRecorder,
    out_stream: TextIO,
    err_stream: TextIO,
) -> int:
    """Render top-level `wt` help when invoked without arguments."""
    token = _ACTIVE_IO_STREAMS.set((out_stream, err_stream))
    try:
        parser.print_help(out_stream)
    finally:
        _ACTIVE_IO_STREAMS.reset(token)
    recorder.record_cli_command(subcommand=SUBCOMMAND_HELP, outcome=OUTCOME_OK)
    return EXIT_SUCCESS


def _execute_parsed_command(
    args: argparse.Namespace,
    settings: WorktrackSettings,
    recorder: TelemetryRecorder,
    env: Mapping[str, str],
    out_stream: TextIO,
    err_stream: TextIO,
) -> int:
    """Run parsed subcommand handler with error formatting and bounded metric recording."""
    ctx = CliContext(
        settings=settings,
        telemetry=recorder,
        environ=env,
        stdout=out_stream,
        stderr=err_stream,
        as_json=bool(getattr(args, "as_json", False)),
    )
    canonical_sub = normalize_cli_subcommand(
        getattr(args, "canonical_subcommand", getattr(args, "subcommand", None))
    )
    handler: Callable[[CliContext, argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        ctx.record_outcome(SUBCOMMAND_HELP, OUTCOME_OK)
        return EXIT_SUCCESS

    try:
        exit_code = int(handler(ctx, args))
    except (
        LedgerError,
        AgendaError,
        OutboxError,
        ConnectorError,
        CalendarConnectorError,
        CollectorError,
        SensorError,
        ReconcilerError,
        McpError,
        ExporterError,
        HubSyncError,
        SecretError,
        ConfigurationError,
        IpcError,
        ValueError,
    ) as exc:
        safe_message = scrub_secrets(str(exc))
        if ctx.as_json:
            write_json(err_stream, {"error": safe_message})
        else:
            write_line(err_stream, f"error: {safe_message}")
        ctx.record_outcome(canonical_sub, OUTCOME_ERROR)
        return EXIT_ERROR

    outcome = OUTCOME_OK if exit_code == EXIT_SUCCESS else OUTCOME_ERROR
    ctx.record_outcome(canonical_sub, outcome)
    return exit_code


# endregion
