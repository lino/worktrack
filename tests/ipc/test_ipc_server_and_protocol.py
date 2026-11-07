"""Behavioural tests for `worktrackd` Unix/TCP IPC server, framing, auth, and telemetry."""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from worktrack.ipc import (
    FRAMING_LENGTH_PREFIXED,
    FRAMING_NEWLINE,
    JSONRPC_AUTH_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    LOOPBACK_IPV4,
    LOOPBACK_IPV6,
    SOCKET_FILE_MODE,
    AgendaItemSummary,
    AgendaTodaySnapshot,
    ClockInRequest,
    ClockOutRequest,
    DaemonStatusSnapshot,
    FocusBlockSummary,
    IpcApplicationError,
    IpcAuthenticationError,
    IpcClient,
    IpcEndpointConfig,
    IpcRpcError,
    IpcTransportError,
    OutboxSummarySnapshot,
    StatusProvider,
    WorktrackIpcServer,
    encode_frame,
    read_frame,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
ENV_SHORT_TMPDIR = "WORKTRACK_TEST_SHORT_TMPDIR"
MAX_SHORT_TMPDIR_LEN = 32
EPHEMERAL_PORT = 0
NON_LOOPBACK_TEST_IP = "192.0.2.10"
VALID_BEARER_TOKEN = "wt-local-test-secret-token-2026"
WRONG_BEARER_TOKEN = "wt-local-wrong-secret-token-0000"
SAMPLE_ELAPSED_SECONDS = 1500
SAMPLE_DAILY_TOTAL_SECONDS = 9000
SAMPLE_BUDGET_SECONDS = 7200
SAMPLE_PENDING_SECONDS = 5400
SAMPLE_STAGED_COUNT = 3
SOCKET_TEST_TIMEOUT = 1.0
ACTIVE_TWO_CLIENTS = 2.0
ZERO_CLIENTS = 0.0
APPLICATION_ERROR_CODE = -32010
NEGATIVE_PORT = -1
OUT_OF_RANGE_HIGH_PORT = 65536
# endregion


# region Fixtures
class FakeLedgerStatusProvider:
    """In-memory `StatusProvider` test double simulating active Ledger & Agenda state."""

    def __init__(self) -> None:
        """Initialize test double with a deterministic idle status snapshot."""
        self.current_status = DaemonStatusSnapshot(
            active_activity_key=None,
            active_activity_title=None,
            active_activity_kind=None,
            active_interval_elapsed_seconds=0,
            daily_total_seconds=SAMPLE_DAILY_TOTAL_SECONDS,
            workplace="home_office",
        )
        self.fail_next_status = False

    def status(self) -> DaemonStatusSnapshot:
        """Return current simulated daemon status or raise domain error if armed."""
        if self.fail_next_status:
            self.fail_next_status = False
            raise IpcApplicationError(
                "Simulated ledger lock contention",
                code=APPLICATION_ERROR_CODE,
            )
        return self.current_status

    def clock_in(self, request: ClockInRequest) -> DaemonStatusSnapshot:
        """Transition simulated daemon status into active tracking."""
        self.current_status = DaemonStatusSnapshot(
            active_activity_key=request.activity_key,
            active_activity_title=request.activity_title or request.activity_key,
            active_activity_kind=request.activity_kind,
            active_interval_elapsed_seconds=SAMPLE_ELAPSED_SECONDS,
            daily_total_seconds=SAMPLE_DAILY_TOTAL_SECONDS + SAMPLE_ELAPSED_SECONDS,
            active_interval_started_at="2026-11-07T09:00:00Z",
            workplace=request.workplace or self.current_status.workplace,
        )
        return self.current_status

    def clock_out(self, request: ClockOutRequest) -> DaemonStatusSnapshot:
        """Close simulated active `Interval` and return updated idle snapshot."""
        _ = request.note
        self.current_status = replace(
            self.current_status,
            active_activity_key=None,
            active_activity_title=None,
            active_activity_kind=None,
            active_interval_elapsed_seconds=0,
            active_interval_started_at=None,
        )
        return self.current_status

    def agenda_today(self) -> AgendaTodaySnapshot:
        """Return deterministic Today `Agenda` items and `FocusBlocks`."""
        return AgendaTodaySnapshot(
            date_iso="2026-11-07",
            items=(
                AgendaItemSummary(
                    activity_key="PROJ-142",
                    title="Implement IPC socket server",
                    budget_seconds=SAMPLE_BUDGET_SECONDS,
                    logged_seconds=SAMPLE_ELAPSED_SECONDS,
                    deadline="2026-11-08",
                ),
            ),
            focus_blocks=(
                FocusBlockSummary(
                    block_id="fb-01",
                    activity_key="PROJ-142",
                    start_at="2026-11-07T09:00:00Z",
                    end_at="2026-11-07T11:00:00Z",
                ),
            ),
            planned_budget_seconds=SAMPLE_BUDGET_SECONDS,
            logged_total_seconds=SAMPLE_DAILY_TOTAL_SECONDS,
            workplace="home_office",
        )

    def outbox_summary(self) -> OutboxSummarySnapshot:
        """Return deterministic staged `Outbox` summary."""
        return OutboxSummarySnapshot(
            staged_count=SAMPLE_STAGED_COUNT,
            pending_seconds=SAMPLE_PENDING_SECONDS,
            sinks=("jira",),
            last_flush_at="2026-11-06T17:30:00Z",
        )


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


# endregion


# region IPC Server Tests
def test_unix_socket_server_permissions_cleanup_and_five_methods(short_sock_dir: Path) -> None:
    """Verify 0600 permissions, stale cleanup, and all 5 RPC methods over Unix socket."""
    provider = FakeLedgerStatusProvider()
    assert isinstance(provider, StatusProvider)
    sock_path = short_sock_dir / "nested" / "worktrackd.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.write_text("stale-marker", encoding="utf-8")

    with WorktrackIpcServer(provider=provider, socket_path=sock_path) as server:
        assert server.bound_socket_path == sock_path
        mode = stat.S_IMODE(sock_path.stat().st_mode)
        assert mode == SOCKET_FILE_MODE

        with pytest.raises(IpcTransportError):
            WorktrackIpcServer(provider=provider, socket_path=sock_path)

        nl_client = IpcClient(IpcEndpointConfig.for_unix(sock_path, framing=FRAMING_NEWLINE))
        lp_client = IpcClient(
            IpcEndpointConfig.for_unix(sock_path, framing=FRAMING_LENGTH_PREFIXED)
        )

        initial = nl_client.status()
        assert not initial.is_tracking
        assert initial.daily_total_seconds == SAMPLE_DAILY_TOTAL_SECONDS

        active = lp_client.clock_in(
            ClockInRequest(activity_key="PROJ-142", activity_title="IPC Server", workplace="office")
        )
        assert active.is_tracking
        assert (active.active_activity_key, active.workplace) == ("PROJ-142", "office")

        agenda = nl_client.agenda_today()
        assert agenda.date_iso == "2026-11-07"
        assert agenda.items[0].activity_key == "PROJ-142"
        assert agenda.focus_blocks[0].block_id == "fb-01"

        outbox = lp_client.outbox_summary()
        assert (outbox.staged_count, outbox.sinks) == (SAMPLE_STAGED_COUNT, ("jira",))

        stopped = nl_client.clock_out(ClockOutRequest(note="Done slice"))
        assert not stopped.is_tracking

    assert not sock_path.exists()


def _assert_tcp_bind_guards(provider: StatusProvider) -> None:
    """Verify non-loopback host and empty TCP bearer token are rejected on bind."""
    with pytest.raises(IpcTransportError):
        WorktrackIpcServer(
            provider=provider,
            tcp_host=NON_LOOPBACK_TEST_IP,
            tcp_port=EPHEMERAL_PORT,
            auth_token=VALID_BEARER_TOKEN,
        )
    with pytest.raises(IpcAuthenticationError):
        WorktrackIpcServer(
            provider=provider,
            tcp_host=LOOPBACK_IPV4,
            tcp_port=EPHEMERAL_PORT,
            auth_token="",
        )


def test_loopback_tcp_enforces_loopback_bind_and_constant_time_bearer_auth(
    tmp_path: Path,
) -> None:
    """Verify TCP server rejects non-loopback hosts and enforces bearer token auth."""
    provider = FakeLedgerStatusProvider()
    recorder = TelemetryRecorder(telemetry_dir=tmp_path / "tcp_telemetry")
    _assert_tcp_bind_guards(provider)

    server = WorktrackIpcServer(
        provider=provider,
        tcp_host=LOOPBACK_IPV4,
        tcp_port=EPHEMERAL_PORT,
        auth_token=VALID_BEARER_TOKEN,
        recorder=recorder,
    )
    with server:
        port = server.bound_port
        assert port > EPHEMERAL_PORT

        unauth_client = IpcClient(IpcEndpointConfig.for_tcp(LOOPBACK_IPV4, port, auth_token=None))
        with pytest.raises(IpcRpcError) as missing_exc:
            unauth_client.status()
        assert missing_exc.value.code == JSONRPC_AUTH_ERROR

        wrong_client = IpcClient(
            IpcEndpointConfig.for_tcp(LOOPBACK_IPV4, port, auth_token=WRONG_BEARER_TOKEN)
        )
        with pytest.raises(IpcRpcError) as wrong_exc:
            wrong_client.status()
        assert wrong_exc.value.code == JSONRPC_AUTH_ERROR

        valid_client = IpcClient(
            IpcEndpointConfig.for_tcp(
                LOOPBACK_IPV4,
                port,
                auth_token=VALID_BEARER_TOKEN,
                framing=FRAMING_LENGTH_PREFIXED,
            )
        )
        snap = valid_client.clock_in(ClockInRequest(activity_key="PROJ-142"))
        assert snap.active_activity_key == "PROJ-142"

    tcp_metrics = recorder.render_metrics_text()
    assert (
        f'worktrack_ipc_request_duration_seconds_count{{method="status",outcome="unauthorized"}} '
        f"{ACTIVE_TWO_CLIENTS}" in tcp_metrics
    )
    assert (
        'worktrack_ipc_request_duration_seconds_count{method="clock_in",outcome="ok"} 1.0'
        in tcp_metrics
    )


def test_ipc_jsonrpc_errors_and_bounded_telemetry_metrics(
    short_sock_dir: Path, tmp_path: Path
) -> None:
    """Verify JSON-RPC error codes, active connection gauge, and bounded method labels."""
    provider = FakeLedgerStatusProvider()
    recorder = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    sock_path = short_sock_dir / "telemetry.sock"

    with WorktrackIpcServer(
        provider=provider,
        socket_path=sock_path,
        recorder=recorder,
    ) as server:
        client = IpcClient(IpcEndpointConfig.for_unix(sock_path))
        _ = client.status()

        with pytest.raises(IpcRpcError) as unknown_exc:
            client.call_raw("PROJ-999-high-cardinality", {})
        assert unknown_exc.value.code == JSONRPC_METHOD_NOT_FOUND

        with pytest.raises(IpcRpcError) as params_exc:
            client.call_raw("clock_in", {"activity_key": ""})
        assert params_exc.value.code == JSONRPC_INVALID_PARAMS

        provider.fail_next_status = True
        with pytest.raises(IpcRpcError) as app_exc:
            client.status()
        assert app_exc.value.code == APPLICATION_ERROR_CODE

        _assert_malformed_json_and_concurrent_connections(sock_path, server, recorder)

    metrics_after_close = recorder.render_metrics_text()
    assert f"worktrack_ipc_active_connections {ZERO_CLIENTS}" in metrics_after_close
    assert (
        f'worktrack_ipc_request_duration_seconds_count{{method="status",outcome="ok"}} '
        f"{ACTIVE_TWO_CLIENTS}" in metrics_after_close
    )
    assert (
        f'worktrack_ipc_request_duration_seconds_count{{method="unknown",outcome="error"}} '
        f"{ACTIVE_TWO_CLIENTS}" in metrics_after_close
    )
    assert (
        'worktrack_ipc_request_duration_seconds_count{method="clock_in",outcome="error"} 1.0'
        in metrics_after_close
    )
    assert "PROJ-999" not in metrics_after_close


def _assert_malformed_json_and_concurrent_connections(
    sock_path: Path,
    server: WorktrackIpcServer,
    recorder: TelemetryRecorder,
) -> None:
    """Verify parse error handling and concurrent `worktrack_ipc_active_connections` gauge."""
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first_conn,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as second_conn,
    ):
        first_conn.settimeout(SOCKET_TEST_TIMEOUT)
        second_conn.settimeout(SOCKET_TEST_TIMEOUT)
        first_conn.connect(str(sock_path))
        second_conn.connect(str(sock_path))

        first_conn.sendall(b"{not-valid-json\n")
        raw_reply = first_conn.makefile("rb").readline().decode("utf-8")
        assert str(JSONRPC_PARSE_ERROR) in raw_reply

        second_conn.sendall(
            encode_frame({"jsonrpc": "2.0", "id": 7, "method": "status", "params": {}})
        )
        _ = second_conn.makefile("rb").readline()

        assert server.active_connections == int(ACTIVE_TWO_CLIENTS)
        live_metrics = recorder.render_metrics_text()
        assert f"worktrack_ipc_active_connections {ACTIVE_TWO_CLIENTS}" in live_metrics


def test_ipv6_loopback_roundtrip_and_tcp_port_bounds() -> None:
    """Verify IPv6 `::1` loopback RPC round-trip and strict `tcp_port` bounds validation."""
    provider = FakeLedgerStatusProvider()
    invalid_ports: tuple[int | bool, ...] = (NEGATIVE_PORT, OUT_OF_RANGE_HIGH_PORT, True)
    for bad_port in invalid_ports:
        with pytest.raises(IpcTransportError):
            WorktrackIpcServer(
                provider=provider,
                tcp_host=LOOPBACK_IPV4,
                tcp_port=bad_port,
                auth_token=VALID_BEARER_TOKEN,
            )

    with WorktrackIpcServer(
        provider=provider,
        tcp_host=LOOPBACK_IPV6,
        tcp_port=EPHEMERAL_PORT,
        auth_token=VALID_BEARER_TOKEN,
    ) as ipv6_server:
        assert ipv6_server.bound_host == LOOPBACK_IPV6
        assert ipv6_server.bound_port > EPHEMERAL_PORT
        client = IpcClient(
            IpcEndpointConfig.for_tcp(
                LOOPBACK_IPV6,
                ipv6_server.bound_port,
                auth_token=VALID_BEARER_TOKEN,
            )
        )
        updated = client.clock_in(ClockInRequest(activity_key="PROJ-142"))
        assert updated.active_activity_key == "PROJ-142"


def test_newline_frame_exact_max_bytes_roundtrips_symmetrically() -> None:
    """Verify `read_frame` accepts payloads of exact `max_frame_bytes` with LF or CRLF."""
    payload: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": "status", "params": {}}
    encoded_lf = encode_frame(payload, framing=FRAMING_NEWLINE)
    exact_body_len = len(encoded_lf) - 1
    encoded_crlf = encoded_lf[:-1] + b"\r\n"

    lf_frame = read_frame(io.BytesIO(encoded_lf), max_frame_bytes=exact_body_len)
    assert lf_frame is not None
    assert (json.loads(lf_frame[0]), lf_frame[1]) == (payload, FRAMING_NEWLINE)

    crlf_frame = read_frame(io.BytesIO(encoded_crlf), max_frame_bytes=exact_body_len)
    assert crlf_frame is not None
    assert (json.loads(crlf_frame[0]), crlf_frame[1]) == (payload, FRAMING_NEWLINE)

    with pytest.raises(IpcTransportError):
        read_frame(io.BytesIO(encoded_lf), max_frame_bytes=exact_body_len - 1)


# endregion
