"""Command-line entrypoint for `python -m worktrack.ipc` status probes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence

from worktrack.config import ConfigurationError
from worktrack.ipc.client import (
    DEFAULT_CLIENT_TIMEOUT_SECONDS,
    DEFAULT_PROMPT_TIMEOUT_SECONDS,
    IpcClient,
    resolve_ipc_endpoint,
)
from worktrack.ipc.models import IpcError
from worktrack.ipc.prompt import format_porcelain_status, probe_prompt_status

EXIT_SUCCESS: int = 0


def build_argument_parser() -> argparse.ArgumentParser:
    """Build argument parser for `python -m worktrack.ipc`."""
    parser = argparse.ArgumentParser(
        prog="python -m worktrack.ipc",
        description="Query local worktrackd IPC status for shell prompts and diagnostics.",
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        default="status",
        choices=("status",),
        help="IPC query command to execute (defaults to 'status').",
    )
    parser.add_argument(
        "--porcelain",
        action="store_true",
        help="Emit single-line prompt output (empty and exit 0 when idle or offline).",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit structured JSON status payload.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Optional connect/read timeout override in seconds.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Execute `worktrack.ipc` CLI status probe and return process exit code."""
    args = build_argument_parser().parse_args(argv)
    if args.porcelain:
        timeout = DEFAULT_PROMPT_TIMEOUT_SECONDS if args.timeout is None else float(args.timeout)
        segment = probe_prompt_status(timeout_seconds=timeout, environ=environ)
        if segment:
            sys.stdout.write(f"{segment}\n")
        return EXIT_SUCCESS

    timeout = DEFAULT_CLIENT_TIMEOUT_SECONDS if args.timeout is None else float(args.timeout)
    return _run_standard_status(as_json=bool(args.as_json), timeout=timeout, environ=environ)


def _run_standard_status(
    *,
    as_json: bool,
    timeout: float,
    environ: Mapping[str, str] | None,
) -> int:
    """Query `worktrackd` status for `--json` or human-readable output."""
    try:
        endpoint = resolve_ipc_endpoint(environ=environ, default_timeout_seconds=timeout)
        snapshot = IpcClient(endpoint).status()
    except (IpcError, ConfigurationError, ValueError, OSError):
        if as_json:
            sys.stdout.write(json.dumps({"online": False, "status": None}) + "\n")
        else:
            sys.stdout.write("worktrackd: offline\n")
        return EXIT_SUCCESS

    if as_json:
        sys.stdout.write(json.dumps({"online": True, "status": snapshot.to_dict()}) + "\n")
    else:
        rendered = format_porcelain_status(snapshot) or "idle"
        sys.stdout.write(f"worktrackd: {rendered}\n")
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
