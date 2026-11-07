"""Threaded `worktrackd` IPC server over POSIX Unix sockets and loopback TCP."""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import time
from pathlib import Path
from types import TracebackType

from worktrack.ipc.dispatch import (
    LOOPBACK_HOSTNAME,
    LOOPBACK_IPV4,
    LOOPBACK_IPV6,
    RUNTIME_DIR_MODE,
    dispatch_jsonrpc_frame,
    extract_bearer_token,
    prepare_unix_socket_path,
    resolve_tcp_socket_target,
    validate_tcp_auth_token,
    validate_tcp_bind_address,
    validate_tcp_port,
    verify_bearer_token,
)
from worktrack.ipc.framing import (
    DEFAULT_MAX_FRAME_BYTES,
    encode_frame,
    read_frame,
)
from worktrack.ipc.models import IpcTransportError, StatusProvider
from worktrack.telemetry import TelemetryRecorder

# region Constants
SOCKET_FILE_MODE = 0o600
DEFAULT_SERVER_BACKLOG = 32
DEFAULT_SERVER_SHUTDOWN_TIMEOUT = 5.0
DEFAULT_ACCEPT_POLL_SECONDS = 0.1
DEFAULT_CLIENT_IDLE_TIMEOUT = 5.0
ACTIVE_CONNECTIONS_METRIC = "worktrack_ipc_active_connections"
UNBOUND_PORT = 0
_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SERVER_BACKLOG",
    "DEFAULT_SERVER_SHUTDOWN_TIMEOUT",
    "LOOPBACK_HOSTNAME",
    "LOOPBACK_IPV4",
    "LOOPBACK_IPV6",
    "RUNTIME_DIR_MODE",
    "SOCKET_FILE_MODE",
    "WorktrackIpcServer",
    "extract_bearer_token",
    "prepare_unix_socket_path",
    "validate_tcp_bind_address",
    "validate_tcp_port",
    "verify_bearer_token",
]
# endregion


# region IPC Server
class WorktrackIpcServer:
    """Threaded JSON-RPC 2.0 server over Unix domain (`0o600`) or authenticated loopback TCP."""

    def __init__(
        self,
        provider: StatusProvider,
        *,
        socket_path: Path | None = None,
        tcp_host: str = LOOPBACK_IPV4,
        tcp_port: int | None = None,
        auth_token: str | None = None,
        allow_non_loopback: bool = False,
        recorder: TelemetryRecorder | None = None,
        shutdown_timeout: float = DEFAULT_SERVER_SHUTDOWN_TIMEOUT,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        """Bind `worktrackd` IPC socket on either POSIX `socket_path` or loopback TCP.

        Args:
            provider: Injectable `StatusProvider` serving canonical IPC methods.
            socket_path: POSIX Unix domain socket path (mutually exclusive with `tcp_port`).
            tcp_host: Loopback IP/hostname when `tcp_port` is specified.
            tcp_port: Loopback TCP port (`0` selects an OS-assigned ephemeral port).
            auth_token: Required secret bearer token when serving TCP requests.
            allow_non_loopback: Explicit escape hatch permitting non-loopback TCP bind.
            recorder: Optional `TelemetryRecorder` tracking connections and durations.
            shutdown_timeout: Graceful thread join timeout in seconds.
            max_frame_bytes: Maximum accepted frame payload size in bytes.
        """
        if (socket_path is None) == (tcp_port is None):
            raise IpcTransportError("Specify exactly one of socket_path or tcp_port.")
        self._provider = provider
        self._recorder = recorder
        self._shutdown_timeout = shutdown_timeout
        self._max_frame_bytes = max_frame_bytes
        self._socket_path = socket_path
        self._is_tcp = tcp_port is not None
        self._auth_token = validate_tcp_auth_token(auth_token) if self._is_tcp else auth_token

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._active_connections = 0
        self._client_sockets: set[socket.socket] = set()
        self._client_threads: set[threading.Thread] = set()
        self._accept_thread: threading.Thread | None = None
        self._listener = self._bind_listener(socket_path, tcp_host, tcp_port, allow_non_loopback)

    @property
    def bound_socket_path(self) -> Path | None:
        """Return bound Unix domain socket path, or `None` in TCP mode."""
        return self._socket_path

    @property
    def bound_host(self) -> str | None:
        """Return bound TCP host address, or `None` in Unix socket mode."""
        if not self._is_tcp:
            return None
        sockname = self._listener.getsockname()
        return str(sockname[0])

    @property
    def bound_port(self) -> int:
        """Return bound TCP port number, or `0` in Unix socket mode."""
        if not self._is_tcp:
            return UNBOUND_PORT
        sockname = self._listener.getsockname()
        return int(sockname[1])

    @property
    def active_connections(self) -> int:
        """Return current count of active concurrent IPC client connections."""
        with self._lock:
            return self._active_connections

    def start_background(self) -> None:
        """Start accepting IPC client connections on a background daemon thread."""
        if self._accept_thread is not None:
            return
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="worktrackd-ipc-accept",
            daemon=True,
        )
        self._accept_thread.start()

    def serve_forever(self) -> None:
        """Run IPC accept loop synchronously in the foreground until `shutdown()`."""
        try:
            self._accept_loop()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Stop accepting clients, close open sockets, join threads, and unlink socket."""
        self._stop_event.set()
        with contextlib.suppress(OSError):
            self._listener.close()

        with self._lock:
            open_clients = list(self._client_sockets)
            worker_threads = list(self._client_threads)
        for client_sock in open_clients:
            with contextlib.suppress(OSError):
                client_sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                client_sock.close()

        if self._accept_thread is not None:
            self._accept_thread.join(timeout=self._shutdown_timeout)
            self._accept_thread = None
        for worker in worker_threads:
            worker.join(timeout=self._shutdown_timeout)

        if self._socket_path is not None:
            self._socket_path.unlink(missing_ok=True)

    def __enter__(self) -> WorktrackIpcServer:
        """Start background IPC server on context entry."""
        self.start_background()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Shut down background IPC server cleanly on context exit."""
        self.shutdown()

    def _bind_listener(
        self,
        socket_path: Path | None,
        tcp_host: str,
        tcp_port: int | None,
        allow_non_loopback: bool,
    ) -> socket.socket:
        """Create, bind, secure (`0o600`), and start listening on the configured socket."""
        if socket_path is not None:
            return _bind_unix_listener(socket_path)
        assert tcp_port is not None
        return _bind_tcp_listener(tcp_host, tcp_port, allow_non_loopback=allow_non_loopback)

    def _accept_loop(self) -> None:
        """Accept incoming client connections and spawn handler threads until stopped."""
        while not self._stop_event.is_set():
            try:
                client_sock, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop_event.is_set():
                    break
                continue
            worker = threading.Thread(
                target=self._serve_client,
                args=(client_sock,),
                name="worktrackd-ipc-client",
                daemon=True,
            )
            with self._lock:
                self._client_sockets.add(client_sock)
                self._client_threads.add(worker)
            worker.start()

    def _serve_client(self, client_sock: socket.socket) -> None:
        """Process framed JSON-RPC requests for a single connected IPC client."""
        self._adjust_active_connections(delta=1)
        client_sock.settimeout(DEFAULT_CLIENT_IDLE_TIMEOUT)
        try:
            with client_sock, client_sock.makefile("rb") as reader:
                while not self._stop_event.is_set():
                    framed = read_frame(reader, max_frame_bytes=self._max_frame_bytes)
                    if framed is None:
                        break
                    raw_bytes, framing = framed
                    response = self._handle_frame(raw_bytes)
                    client_sock.sendall(encode_frame(response, framing=framing))
        except (OSError, IpcTransportError) as exc:
            _LOGGER.debug("IPC client session terminated: %s", exc)
        finally:
            self._adjust_active_connections(delta=-1)
            with self._lock:
                self._client_sockets.discard(client_sock)
                self._client_threads.discard(threading.current_thread())

    def _handle_frame(self, raw_bytes: bytes) -> dict[str, object]:
        """Dispatch a single frame and observe bounded request duration telemetry."""
        started = time.perf_counter()
        response, method_label, outcome = dispatch_jsonrpc_frame(
            raw_bytes,
            provider=self._provider,
            require_tcp_auth=self._is_tcp,
            expected_auth_token=self._auth_token,
        )
        if self._recorder is not None:
            elapsed = max(0.0, time.perf_counter() - started)
            self._recorder.observe_ipc_duration(method_label, outcome, elapsed)
        return response

    def _adjust_active_connections(self, *, delta: int) -> None:
        """Atomically update active connection count and persist Prometheus gauge."""
        with self._lock:
            self._active_connections = max(0, self._active_connections + delta)
            if self._recorder is not None:
                self._recorder.set_gauge(
                    ACTIVE_CONNECTIONS_METRIC,
                    value=float(self._active_connections),
                )


def _bind_unix_listener(socket_path: Path) -> socket.socket:
    """Bind POSIX Unix domain socket with `0o600` permissions and stale cleanup."""
    if not hasattr(socket, "AF_UNIX"):
        raise IpcTransportError("AF_UNIX sockets are not supported on this OS.")
    prepare_unix_socket_path(socket_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, SOCKET_FILE_MODE)
        listener.listen(DEFAULT_SERVER_BACKLOG)
        listener.settimeout(DEFAULT_ACCEPT_POLL_SECONDS)
    except OSError as exc:
        listener.close()
        socket_path.unlink(missing_ok=True)
        raise IpcTransportError(f"Failed to bind Unix socket '{socket_path}': {exc}") from exc
    return listener


def _bind_tcp_listener(
    tcp_host: str,
    tcp_port: int,
    *,
    allow_non_loopback: bool,
) -> socket.socket:
    """Bind validated IPv4 or IPv6 loopback TCP listener socket without leaking descriptors."""
    validated_host = validate_tcp_bind_address(tcp_host, allow_non_loopback=allow_non_loopback)
    validated_port = validate_tcp_port(tcp_port)
    family, sockaddr = resolve_tcp_socket_target(validated_host, validated_port)
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(sockaddr)
        listener.listen(DEFAULT_SERVER_BACKLOG)
        listener.settimeout(DEFAULT_ACCEPT_POLL_SECONDS)
    except Exception as exc:
        listener.close()
        raise IpcTransportError(
            f"Failed to bind TCP {validated_host}:{validated_port}: {exc}"
        ) from exc
    return listener


# endregion
