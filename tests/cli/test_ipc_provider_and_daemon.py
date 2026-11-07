"""Behavioural tests for thread-safe `LedgerIpcServiceProvider`, `wt daemon`, and `wt status`."""

from __future__ import annotations

import gc
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import warnings
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worktrack.agenda import HORIZON_TODAY, AgendaPlanner
from worktrack.cli import EXIT_SUCCESS, LedgerIpcServiceProvider, main
from worktrack.core import OVERLAP_POLICY_CLOSE_PREVIOUS, Ledger
from worktrack.ipc import (
    ClockInRequest,
    ClockOutRequest,
    IpcClient,
    IpcEndpointConfig,
    IpcRpcError,
    IpcServiceProvider,
    OutboxSummarySnapshot,
    WorktrackIpcServer,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
ENV_SHORT_TMPDIR = "WORKTRACK_TEST_SHORT_TMPDIR"
MAX_SHORT_TMPDIR_LEN = 32
OFFLINE_PORCELAIN_BUDGET_SECONDS = 0.015
CONCURRENT_WORKER_COUNT = 8
REQUESTS_PER_WORKER = 6
BUDGET_SECONDS_ONE_HOUR = 3_600
ELAPSED_TEN_MIN_SECONDS = 600
SAMPLE_STAGED_COUNT = 3
SAMPLE_PENDING_SECONDS = 5_400
DAEMON_READY_POLL_SECONDS = 0.02
DAEMON_READY_MAX_ATTEMPTS = 50
DAEMON_JOIN_TIMEOUT_SECONDS = 3.0
# endregion


# region Fixtures
def _resolve_short_socket_base() -> Path:
    """Resolve a short temporary directory path suitable for POSIX `AF_UNIX` limits."""
    override = os.environ.get(ENV_SHORT_TMPDIR, "").strip()
    if override:
        return Path(override)
    default_tmp = Path(tempfile.gettempdir())
    if len(str(default_tmp)) <= MAX_SHORT_TMPDIR_LEN:
        return default_tmp
    posix_short = Path(os.sep, "tmp")
    return posix_short if posix_short.is_dir() else default_tmp


@pytest.fixture
def short_sock_dir() -> Iterator[Path]:
    """Yield a short temporary runtime directory cleaned up on teardown."""
    with tempfile.TemporaryDirectory(prefix="wt_", dir=_resolve_short_socket_base()) as raw_dir:
        yield Path(raw_dir)


def _cli_env(tmp_path: Path, sock_path: Path) -> dict[str, str]:
    """Build isolated environment dictionary pointing CLI to `tmp_path` and `sock_path`."""
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "WORKTRACK_DB_PATH": str(tmp_path / "data" / "ledger.sqlite3"),
        "WORKTRACK_SOCKET_PATH": str(sock_path),
        "WORKTRACK_TELEMETRY_DIR": str(tmp_path / "telemetry"),
    }


def _run_wt(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """Invoke `main` with isolated streams and return `(rc, stdout, stderr)`."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = main(argv, environ=env, stdout=out_buf, stderr=err_buf)
    return rc, out_buf.getvalue(), err_buf.getvalue()


# endregion


# region IPC Provider Tests
def test_ledger_ipc_service_provider_round_trip_and_custom_outbox_callback(
    tmp_path: Path,
    short_sock_dir: Path,
) -> None:
    """Verify `LedgerIpcServiceProvider` serves all 5 IPC methods over `WorktrackIpcServer`."""
    db_path = tmp_path / "ledger.sqlite3"
    sock_path = short_sock_dir / "wt.sock"
    recorder = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    current_time = [datetime(2026, 11, 7, 9, 0, tzinfo=UTC)]

    with (
        Ledger(db_path, telemetry=recorder) as ledger,
        AgendaPlanner(db_path, ledger=ledger, telemetry=recorder) as planner,
    ):
        act = ledger.create_activity("Implement PKCE", external_ref="PROJ-42")
        planner.schedule_item(act.id, horizon=HORIZON_TODAY, budget_seconds=BUDGET_SECONDS_ONE_HOUR)

    provider = LedgerIpcServiceProvider(
        db_path,
        telemetry=recorder,
        clock_fn=lambda: current_time[0],
        outbox_summary_fn=lambda: OutboxSummarySnapshot(
            staged_count=SAMPLE_STAGED_COUNT,
            pending_seconds=SAMPLE_PENDING_SECONDS,
            sinks=("jira",),
        ),
    )
    assert isinstance(provider, IpcServiceProvider)

    with WorktrackIpcServer(provider=provider, socket_path=sock_path, recorder=recorder):
        client = IpcClient(IpcEndpointConfig.for_unix(sock_path))
        assert client.status().is_tracking is False
        started = client.clock_in(
            ClockInRequest(activity_key="PROJ-42", note="Start", workplace="home_office")
        )
        assert (started.active_activity_key, started.workplace) == ("PROJ-42", "home_office")
        with pytest.raises(IpcRpcError):
            client.clock_in(ClockInRequest(activity_key="Other"))

        current_time[0] = current_time[0] + timedelta(seconds=ELAPSED_TEN_MIN_SECONDS)
        assert client.status().active_interval_elapsed_seconds == ELAPSED_TEN_MIN_SECONDS
        assert client.agenda_today().items[0].logged_seconds == ELAPSED_TEN_MIN_SECONDS
        assert client.outbox_summary().staged_count == SAMPLE_STAGED_COUNT
        stopped = client.clock_out(ClockOutRequest(note="Done"))
        assert (stopped.is_tracking, stopped.daily_total_seconds) == (
            False,
            ELAPSED_TEN_MIN_SECONDS,
        )


def test_ledger_ipc_provider_is_thread_safe_across_concurrent_ipc_workers(
    tmp_path: Path,
    short_sock_dir: Path,
) -> None:
    """Verify concurrent `WorktrackIpcServer` worker threads never raise SQLite thread errors."""
    db_path = tmp_path / "concurrent.sqlite3"
    sock_path = short_sock_dir / "concurrent.sock"
    provider = LedgerIpcServiceProvider(db_path, overlap_policy=OVERLAP_POLICY_CLOSE_PREVIOUS)

    def _exercise_worker(worker_idx: int) -> list[bool]:
        """Run sequential IPC RPC calls inside a concurrent client thread."""
        client = IpcClient(IpcEndpointConfig.for_unix(sock_path, timeout_seconds=2.0))
        results: list[bool] = []
        for req_idx in range(REQUESTS_PER_WORKER):
            snap = (
                client.clock_in(ClockInRequest(activity_key="Concurrent Work"))
                if req_idx == 0 and worker_idx == 0
                else client.status()
            )
            _ = client.agenda_today()
            _ = client.outbox_summary()
            results.append(isinstance(snap.daily_total_seconds, int))
        return results

    with (
        WorktrackIpcServer(provider=provider, socket_path=sock_path),
        ThreadPoolExecutor(max_workers=CONCURRENT_WORKER_COUNT) as pool,
    ):
        batches = list(pool.map(_exercise_worker, range(CONCURRENT_WORKER_COUNT)))
    assert len(batches) == CONCURRENT_WORKER_COUNT and all(all(b) for b in batches)


def test_ledger_ipc_provider_closes_sqlite_connections_per_rpc(
    tmp_path: Path,
    short_sock_dir: Path,
) -> None:
    """Verify repeated `IpcClient.status()` polls leak zero SQLite connections or warnings."""
    db_path = tmp_path / "no_leak.sqlite3"
    sock_path = short_sock_dir / "no_leak.sock"
    provider = LedgerIpcServiceProvider(db_path)

    gc.collect()
    baseline_conns = sum(1 for obj in gc.get_objects() if isinstance(obj, sqlite3.Connection))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        with WorktrackIpcServer(provider=provider, socket_path=sock_path):
            client = IpcClient(IpcEndpointConfig.for_unix(sock_path))
            for _ in range(REQUESTS_PER_WORKER):
                assert client.status().is_tracking is False
                _ = client.agenda_today()
        gc.collect()

    resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
    after_conns = sum(1 for obj in gc.get_objects() if isinstance(obj, sqlite3.Connection))
    assert resource_warnings == []
    assert after_conns <= baseline_conns


def test_wt_status_porcelain_exits_under_15ms_when_offline(
    tmp_path: Path,
    short_sock_dir: Path,
) -> None:
    """Verify `wt status --porcelain` exits `0` with empty output in `< 15ms` when offline."""
    env = _cli_env(tmp_path, short_sock_dir / "offline.sock")
    rc_warm, out_warm, _ = _run_wt(["status", "--porcelain"], env)
    assert (rc_warm, out_warm) == (EXIT_SUCCESS, "")

    started = time.perf_counter()
    rc, out_txt, _ = _run_wt(["status", "--porcelain"], env)
    elapsed = time.perf_counter() - started
    assert (rc, out_txt) == (EXIT_SUCCESS, "")
    assert elapsed < OFFLINE_PORCELAIN_BUDGET_SECONDS

    rc_ping, _, _ = _run_wt(["daemon", "ping"], env)
    assert rc_ping != EXIT_SUCCESS


def test_wt_daemon_run_ping_status_and_stop_lifecycle(
    tmp_path: Path,
    short_sock_dir: Path,
) -> None:
    """Verify `wt daemon run/ping/status/stop` and live `wt status --porcelain`."""
    sock_path = short_sock_dir / "daemon.sock"
    env = _cli_env(tmp_path, sock_path)
    assert _run_wt(["in", "Auth Broker", "--ref", "PROJ-77"], env)[0] == EXIT_SUCCESS

    daemon_rc: list[int] = []
    daemon_thread = threading.Thread(
        target=lambda: daemon_rc.append(_run_wt(["daemon", "run"], env)[0]),
        daemon=True,
    )
    daemon_thread.start()

    for _ in range(DAEMON_READY_MAX_ATTEMPTS):
        if sock_path.exists() and _run_wt(["daemon", "ping"], env)[0] == EXIT_SUCCESS:
            break
        time.sleep(DAEMON_READY_POLL_SECONDS)

    rc_porcelain, porcelain_txt, _ = _run_wt(["status", "--porcelain"], env)
    assert rc_porcelain == EXIT_SUCCESS
    assert "PROJ-77" in porcelain_txt and "today" in porcelain_txt

    rc_status, status_json, _ = _run_wt(["daemon", "status", "--json"], env)
    assert rc_status == EXIT_SUCCESS
    parsed = json.loads(status_json)
    assert (parsed["online"], parsed["status"]["active_activity_key"]) == (True, "PROJ-77")

    assert _run_wt(["daemon", "stop"], env)[0] == EXIT_SUCCESS
    daemon_thread.join(timeout=DAEMON_JOIN_TIMEOUT_SECONDS)
    assert not daemon_thread.is_alive() and daemon_rc == [EXIT_SUCCESS]


# endregion
