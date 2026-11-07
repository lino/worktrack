"""Behavioural tests for `IpcClient` endpoint resolution, fast-fail offline probes, and CLI."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from worktrack.ipc import (
    DEFAULT_PROMPT_TIMEOUT_SECONDS,
    ENV_IPC_AUTH_TOKEN,
    ENV_IPC_FRAMING,
    ENV_IPC_HOST,
    ENV_IPC_PORT,
    ENV_IPC_TIMEOUT_SECONDS,
    ENV_IPC_TRANSPORT,
    FRAMING_LENGTH_PREFIXED,
    LOOPBACK_IPV4,
    ClockInRequest,
    DaemonStatusSnapshot,
    IpcRpcError,
    StaticStatusProvider,
    WorktrackIpcServer,
    format_duration_compact,
    format_porcelain_status,
    probe_prompt_status,
    resolve_ipc_endpoint,
)
from worktrack.ipc.__main__ import main as ipc_cli_main
from worktrack.ipc.client import _decode_jsonrpc_reply

# region Constants
ENV_SHORT_TMPDIR = "WORKTRACK_TEST_SHORT_TMPDIR"
MAX_SHORT_TMPDIR_LEN = 32
OFFLINE_FAST_FAIL_BUDGET_SECONDS = 0.015
SAMPLE_CUSTOM_PORT = 19445
SAMPLE_CUSTOM_TIMEOUT = 0.05
DURATION_ZERO = 0
DURATION_SUB_MINUTE = 42
DURATION_TWENTY_FIVE_MIN = 1500
DURATION_EXACT_TWO_HOURS = 7200
DURATION_TWO_HOURS_THIRTY_MIN = 9000
EXIT_OK = 0
EXPECTED_FALLBACK_RPC_CODE = -1
# endregion


# region Fixtures
def _resolve_short_socket_base() -> Path:
    """Resolve a short temporary directory path suitable for `AF_UNIX` bind limits."""
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
    """Yield a short temporary runtime directory cleaned up after the test."""
    with tempfile.TemporaryDirectory(prefix="wt_", dir=_resolve_short_socket_base()) as raw_dir:
        yield Path(raw_dir)


def _find_unused_loopback_port() -> int:
    """Allocate and immediately close an ephemeral TCP port to simulate refused port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
        probe_sock.bind((LOOPBACK_IPV4, 0))
        return int(probe_sock.getsockname()[1])


# endregion


# region IPC Client Tests
def test_resolve_ipc_endpoint_honours_xdg_toml_and_env_overrides(tmp_path: Path) -> None:
    """Verify `resolve_ipc_endpoint` layers WorktrackSettings, TOML, and env variables."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    run_dir = tmp_path / "run"
    default_ep = resolve_ipc_endpoint(
        environ={"XDG_RUNTIME_DIR": str(run_dir)},
        home_dir=home,
    )
    assert default_ep.transport == "unix"
    assert default_ep.socket_path == run_dir / "worktrack" / "worktrackd.sock"

    tcp_ep = resolve_ipc_endpoint(
        environ={
            "XDG_RUNTIME_DIR": str(run_dir),
            ENV_IPC_TRANSPORT: "tcp",
            ENV_IPC_HOST: LOOPBACK_IPV4,
            ENV_IPC_PORT: str(SAMPLE_CUSTOM_PORT),
            ENV_IPC_AUTH_TOKEN: "tok-123",
            ENV_IPC_TIMEOUT_SECONDS: str(SAMPLE_CUSTOM_TIMEOUT),
            ENV_IPC_FRAMING: FRAMING_LENGTH_PREFIXED,
        },
        home_dir=home,
    )
    assert tcp_ep.transport == "tcp"
    assert (tcp_ep.tcp_host, tcp_ep.tcp_port) == (LOOPBACK_IPV4, SAMPLE_CUSTOM_PORT)
    assert tcp_ep.auth_token == "tok-123"
    assert tcp_ep.timeout_seconds == SAMPLE_CUSTOM_TIMEOUT
    assert tcp_ep.framing == FRAMING_LENGTH_PREFIXED


def test_format_duration_and_porcelain_status_outputs() -> None:
    """Verify compact duration formatting and porcelain output across tracking states."""
    assert format_duration_compact(DURATION_ZERO) == "0m"
    assert format_duration_compact(DURATION_SUB_MINUTE) == "42s"
    assert format_duration_compact(DURATION_TWENTY_FIVE_MIN) == "25m"
    assert format_duration_compact(DURATION_EXACT_TWO_HOURS) == "2h"
    assert format_duration_compact(DURATION_TWO_HOURS_THIRTY_MIN) == "2h30m"

    assert format_porcelain_status(None) == ""
    idle = DaemonStatusSnapshot(daily_total_seconds=DURATION_TWO_HOURS_THIRTY_MIN)
    assert format_porcelain_status(idle) == ""

    active = DaemonStatusSnapshot(
        active_activity_key="PROJ-142",
        active_activity_title="IPC Server",
        active_activity_kind="targeted",
        active_interval_elapsed_seconds=DURATION_TWENTY_FIVE_MIN,
        daily_total_seconds=DURATION_TWO_HOURS_THIRTY_MIN,
    )
    rendered = format_porcelain_status(active)
    assert rendered == "PROJ-142 25m | today 2h30m"

    standing = DaemonStatusSnapshot(
        active_activity_key=None,
        active_activity_title="Incident Triage",
        active_activity_kind="standing",
        active_interval_elapsed_seconds=DURATION_EXACT_TWO_HOURS,
        daily_total_seconds=DURATION_TWO_HOURS_THIRTY_MIN,
    )
    assert format_porcelain_status(standing) == "Incident Triage 2h | today 2h30m"


def test_probe_prompt_status_fast_fails_under_15ms_when_offline(short_sock_dir: Path) -> None:
    """Verify missing Unix socket and refused TCP port return '' in < 15ms with zero errors."""
    missing_sock = short_sock_dir / "absent_worktrackd.sock"
    # Warm import/socket path once, then measure fast-fail offline probe latency.
    _ = probe_prompt_status(
        socket_path=missing_sock,
        timeout_seconds=DEFAULT_PROMPT_TIMEOUT_SECONDS,
    )

    t0 = time.perf_counter()
    unix_result = probe_prompt_status(
        socket_path=missing_sock,
        timeout_seconds=DEFAULT_PROMPT_TIMEOUT_SECONDS,
    )
    unix_elapsed = time.perf_counter() - t0
    assert unix_result == ""
    assert unix_elapsed < OFFLINE_FAST_FAIL_BUDGET_SECONDS

    closed_port = _find_unused_loopback_port()
    _ = probe_prompt_status(
        tcp_host=LOOPBACK_IPV4,
        tcp_port=closed_port,
        auth_token="tok",
        timeout_seconds=DEFAULT_PROMPT_TIMEOUT_SECONDS,
    )
    t1 = time.perf_counter()
    tcp_result = probe_prompt_status(
        tcp_host=LOOPBACK_IPV4,
        tcp_port=closed_port,
        auth_token="tok",
        timeout_seconds=DEFAULT_PROMPT_TIMEOUT_SECONDS,
    )
    tcp_elapsed = time.perf_counter() - t1
    assert tcp_result == ""
    assert tcp_elapsed < OFFLINE_FAST_FAIL_BUDGET_SECONDS


def test_ipc_cli_status_porcelain_and_json_modes(
    short_sock_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify `python -m worktrack.ipc status --porcelain` & `--json` online and offline."""
    sock_path = short_sock_dir / "cli.sock"
    offline_env = {"WORKTRACK_SOCKET_PATH": str(sock_path)}

    rc_offline = ipc_cli_main(["status", "--porcelain"], environ=offline_env)
    assert rc_offline == EXIT_OK
    assert capsys.readouterr().out == ""

    provider = StaticStatusProvider()
    provider.clock_in(ClockInRequest(activity_key="PROJ-142", activity_title="Shell Prompt"))
    with WorktrackIpcServer(provider=provider, socket_path=sock_path):
        rc_online = ipc_cli_main(["status", "--porcelain"], environ=offline_env)
        assert rc_online == EXIT_OK
        porcelain_out = capsys.readouterr().out.strip()
        assert "PROJ-142" in porcelain_out

        rc_json = ipc_cli_main(["status", "--json"], environ=offline_env)
        assert rc_json == EXIT_OK
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["online"] is True
        assert parsed["status"]["active_activity_key"] == "PROJ-142"


def test_decode_jsonrpc_reply_rejects_boolean_error_code() -> None:
    """Verify boolean `error.code` falls back to `-1` instead of coercing `True` to `1`."""
    raw_bool_err = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": True, "message": "boolean code"}}
    ).encode("utf-8")
    with pytest.raises(IpcRpcError) as exc_info:
        _decode_jsonrpc_reply(raw_bool_err)
    assert exc_info.value.code == EXPECTED_FALLBACK_RPC_CODE


# endregion
