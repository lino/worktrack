"""Worktrack (`wt`) command-line interface and `LedgerIpcServiceProvider`."""

from __future__ import annotations

from worktrack.cli.commands_capture import register_capture_subparsers
from worktrack.cli.commands_ops import register_ops_subparsers
from worktrack.cli.commands_outbox import register_outbox_subparsers, use_cli_sinks
from worktrack.cli.commands_planning import register_planning_subparsers
from worktrack.cli.commands_web import (
    register_web_subparsers,
    serve_wsgi_until_stopped,
    use_jira_transport,
)
from worktrack.cli.context import (
    CANONICAL_CLI_SUBCOMMANDS,
    EXIT_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    SUBCOMMAND_AGENDA,
    SUBCOMMAND_DAEMON,
    SUBCOMMAND_FACT,
    SUBCOMMAND_HELP,
    SUBCOMMAND_IN,
    SUBCOMMAND_JIRA,
    SUBCOMMAND_LOG,
    SUBCOMMAND_METRICS,
    SUBCOMMAND_NOW,
    SUBCOMMAND_OUT,
    SUBCOMMAND_OUTBOX,
    SUBCOMMAND_SERVE,
    SUBCOMMAND_SPLIT,
    SUBCOMMAND_STATUS,
    SUBCOMMAND_TODAY,
    SUBCOMMAND_UNKNOWN,
    SUBCOMMAND_WEEK,
    CliContext,
    normalize_cli_subcommand,
    parse_cli_timestamp,
    resolve_or_create_activity,
)
from worktrack.cli.ipc_provider import (
    LedgerIpcServiceProvider,
    build_daemon_status_snapshot,
    build_outbox_summary_snapshot,
)
from worktrack.cli.parser import (
    DEFAULT_SUBPARSER_REGISTRARS,
    StreamArgumentParser,
    SubparserRegistrar,
    build_argument_parser,
    build_json_parent_parser,
    main,
)

__all__ = [
    "CANONICAL_CLI_SUBCOMMANDS",
    "DEFAULT_SUBPARSER_REGISTRARS",
    "EXIT_ERROR",
    "EXIT_SUCCESS",
    "EXIT_USAGE_ERROR",
    "SUBCOMMAND_AGENDA",
    "SUBCOMMAND_DAEMON",
    "SUBCOMMAND_FACT",
    "SUBCOMMAND_HELP",
    "SUBCOMMAND_IN",
    "SUBCOMMAND_JIRA",
    "SUBCOMMAND_LOG",
    "SUBCOMMAND_METRICS",
    "SUBCOMMAND_NOW",
    "SUBCOMMAND_OUT",
    "SUBCOMMAND_OUTBOX",
    "SUBCOMMAND_SERVE",
    "SUBCOMMAND_SPLIT",
    "SUBCOMMAND_STATUS",
    "SUBCOMMAND_TODAY",
    "SUBCOMMAND_UNKNOWN",
    "SUBCOMMAND_WEEK",
    "CliContext",
    "LedgerIpcServiceProvider",
    "StreamArgumentParser",
    "SubparserRegistrar",
    "build_argument_parser",
    "build_daemon_status_snapshot",
    "build_json_parent_parser",
    "build_outbox_summary_snapshot",
    "main",
    "normalize_cli_subcommand",
    "parse_cli_timestamp",
    "register_capture_subparsers",
    "register_ops_subparsers",
    "register_outbox_subparsers",
    "register_planning_subparsers",
    "register_web_subparsers",
    "resolve_or_create_activity",
    "serve_wsgi_until_stopped",
    "use_cli_sinks",
    "use_jira_transport",
]
