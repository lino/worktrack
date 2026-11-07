"""Subparser builders and command handlers for `wt daemon`, `wt status`, and `wt metrics`."""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Any, Final

from worktrack.cli.context import (
    EXIT_ERROR,
    EXIT_SUCCESS,
    OUTCOME_OK,
    SUBCOMMAND_DAEMON,
    SUBCOMMAND_METRICS,
    SUBCOMMAND_STATUS,
    CliContext,
)
from worktrack.cli.ipc_provider import LedgerIpcServiceProvider, build_daemon_status_snapshot
from worktrack.cli.render import render_status_human, write_json, write_line
from worktrack.config import ConfigurationError
from worktrack.core.models import ALLOWED_OVERLAP_POLICIES, OVERLAP_POLICY_FORBID
from worktrack.ipc.client import (
    DEFAULT_CLIENT_TIMEOUT_SECONDS,
    DEFAULT_PROMPT_TIMEOUT_SECONDS,
    TRANSPORT_UNIX,
    IpcClient,
    IpcEndpointConfig,
    resolve_ipc_endpoint,
)
from worktrack.ipc.models import DaemonStatusSnapshot, IpcError
from worktrack.ipc.prompt import probe_prompt_status
from worktrack.ipc.server import LOOPBACK_IPV4, RUNTIME_DIR_MODE, WorktrackIpcServer
from worktrack.telemetry.server import MetricsHttpServer

# region Constants
DAEMON_ACTION_RUN: Final = "run"
DAEMON_ACTION_PING: Final = "ping"
DAEMON_ACTION_STATUS: Final = "status"
DAEMON_ACTION_STOP: Final = "stop"

PID_FILE_MODE: Final = 0o600
PID_FILENAME_SUFFIX: Final = ".pid"
STOP_POLL_INTERVAL_SECONDS: Final = 0.02
STOP_MAX_WAIT_SECONDS: Final = 2.0

_DAEMON_LOCK = threading.Lock()
_ACTIVE_DAEMONS: dict[str, WorktrackIpcServer] = {}
# endregion


# region Subcommands
def register_ops_subparsers(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Register `daemon`, `status`, and `metrics` CLI subcommands."""
    _register_daemon_parser(subparsers, json_parent)
    _register_status_parser(subparsers, json_parent)
    _register_metrics_parser(subparsers, json_parent)


def _register_daemon_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt daemon {run,ping,status,stop}` nested subparsers."""
    daemon_p = subparsers.add_parser(
        SUBCOMMAND_DAEMON,
        parents=[json_parent],
        help="Run, ping, inspect, or stop local worktrackd IPC daemon.",
    )
    daemon_p.set_defaults(handler=handle_daemon, canonical_subcommand=SUBCOMMAND_DAEMON)
    nested = daemon_p.add_subparsers(dest="daemon_action", required=True)

    run_p = nested.add_parser(
        DAEMON_ACTION_RUN,
        parents=[json_parent],
        help="Run worktrackd IPC server in the foreground.",
    )
    run_p.add_argument("--socket", default=None, help="Optional Unix domain socket path override.")
    run_p.add_argument("--tcp-port", type=int, default=None, help="Optional loopback TCP port.")
    run_p.add_argument("--tcp-host", default=LOOPBACK_IPV4, help="Loopback TCP bind host.")
    run_p.add_argument("--auth-token", default=None, help="Bearer token for TCP IPC mode.")
    run_p.add_argument(
        "--overlap-policy",
        default=OVERLAP_POLICY_FORBID,
        choices=sorted(ALLOWED_OVERLAP_POLICIES),
        help="Overlap policy applied to IPC clock_in requests.",
    )

    for action_name, desc in (
        (DAEMON_ACTION_PING, "Check whether local worktrackd is reachable."),
        (DAEMON_ACTION_STATUS, "Query worktrackd IPC status and agenda summary."),
        (DAEMON_ACTION_STOP, "Gracefully stop a running local worktrackd instance."),
    ):
        sub_p = nested.add_parser(action_name, parents=[json_parent], help=desc)
        sub_p.add_argument("--timeout", type=float, default=None, help="IPC timeout in seconds.")


def _register_status_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt status` subparser."""
    status_p = subparsers.add_parser(
        SUBCOMMAND_STATUS,
        parents=[json_parent],
        help="Query active timer status (fast-fail porcelain for shell prompts).",
    )
    status_p.add_argument(
        "--porcelain",
        action="store_true",
        help="Emit compact shell prompt segment; empty and exit 0 when idle or offline.",
    )
    status_p.add_argument("--timeout", type=float, default=None, help="IPC probe timeout (sec).")
    status_p.set_defaults(handler=handle_status, canonical_subcommand=SUBCOMMAND_STATUS)


def _register_metrics_parser(
    subparsers: argparse._SubParsersAction[Any],
    json_parent: argparse.ArgumentParser,
) -> None:
    """Configure `wt metrics` subparser."""
    metrics_p = subparsers.add_parser(
        SUBCOMMAND_METRICS,
        parents=[json_parent],
        help="Render Prometheus OpenMetrics exposition or serve /metrics HTTP endpoint.",
    )
    metrics_p.add_argument(
        "--serve",
        action="store_true",
        help="Start HTTP server exposing /metrics and /healthz.",
    )
    metrics_p.add_argument("--host", default=None, help="Optional bind host override.")
    metrics_p.add_argument("--port", type=int, default=None, help="Optional TCP port override.")
    metrics_p.set_defaults(handler=handle_metrics, canonical_subcommand=SUBCOMMAND_METRICS)


# endregion


# region CLI
def handle_status(ctx: CliContext, args: argparse.Namespace) -> int:
    """Execute `wt status` (`--porcelain` fast path or standard `--json`/human view)."""
    if args.porcelain:
        timeout = DEFAULT_PROMPT_TIMEOUT_SECONDS if args.timeout is None else float(args.timeout)
        segment = probe_prompt_status(timeout_seconds=timeout, environ=ctx.environ)
        if segment:
            write_line(ctx.stdout, segment)
        return EXIT_SUCCESS

    timeout = DEFAULT_CLIENT_TIMEOUT_SECONDS if args.timeout is None else float(args.timeout)
    online, snapshot = _probe_or_fallback_status(ctx, timeout=timeout)
    if ctx.as_json:
        write_json(ctx.stdout, {"online": online, "status": snapshot.to_dict()})
    else:
        render_status_human(ctx.stdout, online=online, snapshot=snapshot)
    return EXIT_SUCCESS


def _probe_or_fallback_status(
    ctx: CliContext,
    *,
    timeout: float,
) -> tuple[bool, DaemonStatusSnapshot]:
    """Query live `worktrackd` status or fall back to local SQLite ledger snapshot."""
    try:
        endpoint = resolve_ipc_endpoint(environ=ctx.environ, default_timeout_seconds=timeout)
        return True, IpcClient(endpoint).status()
    except (IpcError, ConfigurationError, ValueError, OSError):
        return False, _read_offline_status_snapshot(ctx)


def _read_offline_status_snapshot(ctx: CliContext) -> DaemonStatusSnapshot:
    """Build `DaemonStatusSnapshot` from local SQLite ledger when `worktrackd` is offline."""
    if not ctx.settings.db_path.exists():
        return DaemonStatusSnapshot()
    with ctx.open_ledger() as ledger, ctx.open_planner(ledger=ledger) as planner:
        return build_daemon_status_snapshot(ledger, planner)


def handle_metrics(ctx: CliContext, args: argparse.Namespace) -> int:
    """Render Prometheus metrics text or run `MetricsHttpServer`."""
    ctx.record_outcome(SUBCOMMAND_METRICS, OUTCOME_OK)
    if args.serve:
        host = ctx.settings.metrics_host if args.host is None else str(args.host)
        port = ctx.settings.metrics_port if args.port is None else int(args.port)
        server = MetricsHttpServer(
            recorder=ctx.telemetry,
            host=host,
            port=port,
            shutdown_timeout=ctx.settings.metrics_shutdown_timeout,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return EXIT_SUCCESS
        return EXIT_SUCCESS

    exposition = ctx.telemetry.render_metrics_text()
    if ctx.as_json:
        write_json(ctx.stdout, {"metrics": exposition})
    else:
        ctx.stdout.write(exposition)
    return EXIT_SUCCESS


def handle_daemon(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch `wt daemon {run,ping,status,stop}`."""
    action = str(args.daemon_action)
    if action == DAEMON_ACTION_RUN:
        return _handle_daemon_run(ctx, args)
    if action == DAEMON_ACTION_PING:
        return _handle_daemon_ping(ctx, args)
    if action == DAEMON_ACTION_STATUS:
        return _handle_daemon_status(ctx, args)
    return _handle_daemon_stop(ctx)


def _handle_daemon_run(ctx: CliContext, args: argparse.Namespace) -> int:
    """Bind `WorktrackIpcServer` to `LedgerIpcServiceProvider` and serve foreground."""
    provider = LedgerIpcServiceProvider.from_settings(
        ctx.settings,
        telemetry=ctx.telemetry,
        overlap_policy=str(args.overlap_policy),
        environ=ctx.environ,
    )
    server, registry_key, pid_path = _create_daemon_server(ctx, provider, args)
    with _DAEMON_LOCK:
        _ACTIVE_DAEMONS[registry_key] = server
    if pid_path is not None:
        _write_pid_file(pid_path)

    prev_sigterm = _install_sigterm_shutdown(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        _restore_sigterm_handler(prev_sigterm)
        with _DAEMON_LOCK:
            _ACTIVE_DAEMONS.pop(registry_key, None)
        if pid_path is not None:
            pid_path.unlink(missing_ok=True)
    return EXIT_SUCCESS


def _create_daemon_server(
    ctx: CliContext,
    provider: LedgerIpcServiceProvider,
    args: argparse.Namespace,
) -> tuple[WorktrackIpcServer, str, Path | None]:
    """Construct Unix or TCP `WorktrackIpcServer` and derive registry/PID targets."""
    if args.tcp_port is not None:
        server = WorktrackIpcServer(
            provider,
            tcp_host=str(args.tcp_host),
            tcp_port=int(args.tcp_port),
            auth_token=args.auth_token,
            recorder=ctx.telemetry,
        )
        key = f"tcp://{server.bound_host}:{server.bound_port}"
        return server, key, None

    endpoint = resolve_ipc_endpoint(environ=ctx.environ)
    if args.socket is not None:
        sock_path = Path(str(args.socket))
    elif endpoint.transport == TRANSPORT_UNIX and endpoint.socket_path is not None:
        sock_path = endpoint.socket_path
    else:
        server = WorktrackIpcServer(
            provider,
            tcp_host=endpoint.tcp_host,
            tcp_port=endpoint.tcp_port,
            auth_token=endpoint.auth_token,
            recorder=ctx.telemetry,
        )
        key = f"tcp://{server.bound_host}:{server.bound_port}"
        return server, key, None

    server = WorktrackIpcServer(provider, socket_path=sock_path, recorder=ctx.telemetry)
    pid_path = _pid_file_for_socket(sock_path)
    return server, str(sock_path), pid_path


def _handle_daemon_ping(ctx: CliContext, args: argparse.Namespace) -> int:
    """Ping `worktrackd` over IPC and return `0` when online or `1` when offline."""
    timeout = DEFAULT_CLIENT_TIMEOUT_SECONDS if args.timeout is None else float(args.timeout)
    try:
        endpoint = resolve_ipc_endpoint(environ=ctx.environ, default_timeout_seconds=timeout)
        snapshot = IpcClient(endpoint).status()
    except (IpcError, ConfigurationError, ValueError, OSError) as exc:
        if ctx.as_json:
            write_json(ctx.stdout, {"online": False, "error": str(exc)})
        else:
            write_line(ctx.stderr, "worktrackd: offline")
        return EXIT_ERROR

    if ctx.as_json:
        write_json(ctx.stdout, {"online": True, "status": snapshot.to_dict()})
    else:
        write_line(ctx.stdout, "pong")
    return EXIT_SUCCESS


def _handle_daemon_status(ctx: CliContext, args: argparse.Namespace) -> int:
    """Query `worktrackd` `status`, `agenda_today`, and `outbox_summary`."""
    timeout = DEFAULT_CLIENT_TIMEOUT_SECONDS if args.timeout is None else float(args.timeout)
    try:
        endpoint = resolve_ipc_endpoint(environ=ctx.environ, default_timeout_seconds=timeout)
        client = IpcClient(endpoint)
        status_snap = client.status()
        agenda_snap = client.agenda_today()
        outbox_snap = client.outbox_summary()
    except (IpcError, ConfigurationError, ValueError, OSError):
        if ctx.as_json:
            write_json(ctx.stdout, {"online": False, "status": None})
        else:
            write_line(ctx.stdout, "worktrackd: offline")
        return EXIT_SUCCESS

    if ctx.as_json:
        write_json(
            ctx.stdout,
            {
                "online": True,
                "status": status_snap.to_dict(),
                "agenda_today": agenda_snap.to_dict(),
                "outbox_summary": outbox_snap.to_dict(),
            },
        )
    else:
        render_status_human(ctx.stdout, online=True, snapshot=status_snap)
    return EXIT_SUCCESS


def _handle_daemon_stop(ctx: CliContext) -> int:
    """Stop an in-process or PID-tracked local `worktrackd` daemon gracefully."""
    endpoint = resolve_ipc_endpoint(environ=ctx.environ)
    key = (
        str(endpoint.socket_path)
        if endpoint.transport == TRANSPORT_UNIX and endpoint.socket_path is not None
        else f"tcp://{endpoint.tcp_host}:{endpoint.tcp_port}"
    )
    with _DAEMON_LOCK:
        in_proc = _ACTIVE_DAEMONS.get(key)
    if in_proc is not None:
        in_proc.shutdown()
        _emit_stop_result(ctx, stopped=True)
        return EXIT_SUCCESS

    if endpoint.socket_path is not None and _stop_external_pid(endpoint):
        _emit_stop_result(ctx, stopped=True)
        return EXIT_SUCCESS

    _emit_stop_result(ctx, stopped=False)
    return EXIT_SUCCESS


def _stop_external_pid(endpoint: IpcEndpointConfig) -> bool:
    """Send `SIGTERM` to a live external `worktrackd` PID and await socket cleanup."""
    assert endpoint.socket_path is not None
    pid_path = _pid_file_for_socket(endpoint.socket_path)
    if not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pid_path.unlink(missing_ok=True)
        return False
    deadline = time.perf_counter() + STOP_MAX_WAIT_SECONDS
    while endpoint.socket_path.exists() and time.perf_counter() < deadline:
        time.sleep(STOP_POLL_INTERVAL_SECONDS)
    return True


def _emit_stop_result(ctx: CliContext, *, stopped: bool) -> None:
    """Write `wt daemon stop` confirmation to `stdout`."""
    if ctx.as_json:
        write_json(ctx.stdout, {"stopped": stopped})
    else:
        write_line(ctx.stdout, "worktrackd: stopped" if stopped else "worktrackd: offline")


def _pid_file_for_socket(sock_path: Path) -> Path:
    """Derive companion `.pid` path adjacent to `sock_path`."""
    return sock_path.with_name(f"{sock_path.name}{PID_FILENAME_SUFFIX}")


def _write_pid_file(pid_path: Path) -> None:
    """Write current process PID with `0o600` permissions."""
    pid_path.parent.mkdir(parents=True, exist_ok=True, mode=RUNTIME_DIR_MODE)
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(pid_path, PID_FILE_MODE)


def _install_sigterm_shutdown(server: WorktrackIpcServer) -> Any:
    """Install `SIGTERM` handler when executing on the main interpreter thread."""
    if threading.current_thread() is not threading.main_thread():
        return None

    def _on_sigterm(_signum: int, _frame: FrameType | None) -> None:
        """Trigger graceful `WorktrackIpcServer` shutdown on `SIGTERM`."""
        server.shutdown()

    return signal.signal(signal.SIGTERM, _on_sigterm)


def _restore_sigterm_handler(previous: Any) -> None:
    """Restore previous `SIGTERM` handler when installed on the main thread."""
    if previous is not None and threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, previous)


# endregion
