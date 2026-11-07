"""Fast-fail Unix domain and authenticated loopback TCP JSON-RPC 2.0 client."""

from __future__ import annotations

import itertools
import json
import math
import os
import socket
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from worktrack.config import (
    APP_SLUG,
    MAX_TCP_PORT,
    MIN_TCP_PORT,
    ConfigurationError,
    WorktrackSettings,
)
from worktrack.ipc.framing import (
    ALLOWED_FRAMING_MODES,
    DEFAULT_MAX_FRAME_BYTES,
    FRAME_ENCODING,
    FRAMING_NEWLINE,
    JSONRPC_VERSION,
    METHOD_AGENDA_TODAY,
    METHOD_CLOCK_IN,
    METHOD_CLOCK_OUT,
    METHOD_OUTBOX_SUMMARY,
    METHOD_STATUS,
    encode_frame,
    read_frame,
)
from worktrack.ipc.models import (
    AgendaTodaySnapshot,
    ClockInRequest,
    ClockOutRequest,
    DaemonStatusSnapshot,
    IpcRpcError,
    IpcTransportError,
    OutboxSummarySnapshot,
)
from worktrack.ipc.server import LOOPBACK_IPV4

# region Constants
TRANSPORT_UNIX = "unix"
TRANSPORT_TCP = "tcp"
TRANSPORT_AUTO = "auto"
ALLOWED_TRANSPORTS: frozenset[str] = frozenset({TRANSPORT_UNIX, TRANSPORT_TCP, TRANSPORT_AUTO})

DEFAULT_CLIENT_TIMEOUT_SECONDS: float = 0.250
DEFAULT_PROMPT_TIMEOUT_SECONDS: float = 0.010
MIN_TIMEOUT_SECONDS: float = 0.001
DEFAULT_UNKNOWN_RPC_ERROR_CODE: int = -1

ENV_IPC_TRANSPORT = "WORKTRACK_IPC_TRANSPORT"
ENV_IPC_HOST = "WORKTRACK_IPC_HOST"
ENV_IPC_PORT = "WORKTRACK_IPC_PORT"
ENV_IPC_AUTH_TOKEN = "WORKTRACK_IPC_AUTH_TOKEN"
ENV_IPC_TIMEOUT_SECONDS = "WORKTRACK_IPC_TIMEOUT_SECONDS"
ENV_IPC_FRAMING = "WORKTRACK_IPC_FRAMING"
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class IpcEndpointConfig:
    """Resolved client connection target and framing configuration for `worktrackd`.

    Attributes:
        transport: Transport family (`unix` or `tcp`).
        socket_path: Unix domain socket path when `transport == 'unix'`.
        tcp_host: Loopback IP or hostname when `transport == 'tcp'`.
        tcp_port: Loopback TCP port when `transport == 'tcp'`.
        auth_token: Optional bearer token sent on loopback TCP requests.
        timeout_seconds: Connect and read socket timeout in seconds.
        framing: Wire framing mode (`newline` or `length_prefixed`).
    """

    transport: str = TRANSPORT_UNIX
    socket_path: Path | None = None
    tcp_host: str = LOOPBACK_IPV4
    tcp_port: int | None = None
    auth_token: str | None = None
    timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS
    framing: str = FRAMING_NEWLINE

    @classmethod
    def for_unix(
        cls,
        socket_path: Path,
        *,
        timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
        framing: str = FRAMING_NEWLINE,
    ) -> IpcEndpointConfig:
        """Construct an `IpcEndpointConfig` targeting a POSIX Unix domain socket."""
        return cls(
            transport=TRANSPORT_UNIX,
            socket_path=socket_path,
            timeout_seconds=timeout_seconds,
            framing=framing,
        )

    @classmethod
    def for_tcp(
        cls,
        tcp_host: str,
        tcp_port: int,
        *,
        auth_token: str | None = None,
        timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
        framing: str = FRAMING_NEWLINE,
    ) -> IpcEndpointConfig:
        """Construct an `IpcEndpointConfig` targeting a loopback TCP listener."""
        return cls(
            transport=TRANSPORT_TCP,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            auth_token=auth_token,
            timeout_seconds=timeout_seconds,
            framing=framing,
        )


# endregion


# region Resolution
def resolve_ipc_endpoint(
    *,
    environ: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
    default_timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
) -> IpcEndpointConfig:
    """Resolve `IpcEndpointConfig` across XDG `WorktrackSettings`, TOML, and env layers."""
    env = os.environ if environ is None else environ
    settings = WorktrackSettings.from_env(environ=env, home_dir=home_dir)
    toml_ipc = _load_ipc_toml_section(settings.config_file)

    raw_transport = _resolve_str(ENV_IPC_TRANSPORT, "ipc_transport", TRANSPORT_AUTO, env, toml_ipc)
    transport = raw_transport.lower()
    if transport not in ALLOWED_TRANSPORTS:
        raise ConfigurationError(f"Unsupported IPC transport '{raw_transport}'.")

    tcp_host = _resolve_str(ENV_IPC_HOST, "ipc_host", LOOPBACK_IPV4, env, toml_ipc)
    tcp_port = _resolve_optional_port(ENV_IPC_PORT, "ipc_port", env, toml_ipc)
    auth_token = _resolve_optional_str(ENV_IPC_AUTH_TOKEN, "ipc_auth_token", env, toml_ipc)
    timeout = _resolve_timeout(
        ENV_IPC_TIMEOUT_SECONDS, "ipc_timeout_seconds", default_timeout_seconds, env, toml_ipc
    )
    framing = _resolve_str(ENV_IPC_FRAMING, "ipc_framing", FRAMING_NEWLINE, env, toml_ipc).lower()
    if framing not in ALLOWED_FRAMING_MODES:
        raise ConfigurationError(f"Unsupported IPC framing '{framing}'.")

    effective_transport = _select_effective_transport(transport, tcp_port)
    if effective_transport == TRANSPORT_TCP and tcp_port is None:
        raise ConfigurationError("TCP IPC transport requires WORKTRACK_IPC_PORT or ipc_port.")

    return IpcEndpointConfig(
        transport=effective_transport,
        socket_path=settings.socket_path if effective_transport == TRANSPORT_UNIX else None,
        tcp_host=tcp_host,
        tcp_port=tcp_port,
        auth_token=auth_token,
        timeout_seconds=timeout,
        framing=framing,
    )


def _select_effective_transport(transport: str, tcp_port: int | None) -> str:
    """Choose concrete `unix` or `tcp` transport when `auto` is configured."""
    if transport != TRANSPORT_AUTO:
        return transport
    if tcp_port is not None or not hasattr(socket, "AF_UNIX"):
        return TRANSPORT_TCP
    return TRANSPORT_UNIX


def _load_ipc_toml_section(config_file: Path) -> dict[str, object]:
    """Read optional `[worktrack]` table keys from `config.toml` when present."""
    if not config_file.is_file():
        return {}
    raw_doc = tomllib.loads(config_file.read_text(encoding="utf-8"))
    scoped = raw_doc.get(APP_SLUG)
    return dict(scoped) if isinstance(scoped, Mapping) else dict(raw_doc)


def _resolve_str(
    env_key: str,
    toml_key: str,
    default: str,
    env: Mapping[str, str],
    table: Mapping[str, object],
) -> str:
    """Resolve a non-empty string setting with env > TOML > default precedence."""
    override = _resolve_optional_str(env_key, toml_key, env, table)
    return default if override is None else override


def _resolve_optional_str(
    env_key: str,
    toml_key: str,
    env: Mapping[str, str],
    table: Mapping[str, object],
) -> str | None:
    """Resolve an optional string setting from environment or TOML."""
    env_val = env.get(env_key, "").strip()
    if env_val:
        return env_val
    if toml_key in table:
        raw_val = table[toml_key]
        if not isinstance(raw_val, str):
            raise ConfigurationError(f"TOML key '{toml_key}' must be a string.")
        return raw_val.strip() or None
    return None


def _resolve_optional_port(
    env_key: str,
    toml_key: str,
    env: Mapping[str, str],
    table: Mapping[str, object],
) -> int | None:
    """Resolve an optional validated TCP port number."""
    env_val = env.get(env_key, "").strip()
    port: int | None = None
    if env_val:
        try:
            port = int(env_val)
        except ValueError as exc:
            raise ConfigurationError(f"{env_key} must be an integer TCP port.") from exc
    elif toml_key in table:
        raw_val = table[toml_key]
        if isinstance(raw_val, bool) or not isinstance(raw_val, int):
            raise ConfigurationError(f"TOML key '{toml_key}' must be an integer.")
        port = raw_val
    if port is not None and not (MIN_TCP_PORT <= port <= MAX_TCP_PORT):
        raise ConfigurationError(f"IPC TCP port must be between {MIN_TCP_PORT} and {MAX_TCP_PORT}.")
    return port


def _resolve_timeout(
    env_key: str,
    toml_key: str,
    default: float,
    env: Mapping[str, str],
    table: Mapping[str, object],
) -> float:
    """Resolve a positive finite IPC socket timeout in seconds."""
    env_val = env.get(env_key, "").strip()
    timeout = default
    if env_val:
        try:
            timeout = float(env_val)
        except ValueError as exc:
            raise ConfigurationError(f"{env_key} must be a floating-point number.") from exc
    elif toml_key in table:
        raw_val = table[toml_key]
        if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
            raise ConfigurationError(f"TOML key '{toml_key}' must be a number.")
        timeout = float(raw_val)
    if not math.isfinite(timeout) or timeout < MIN_TIMEOUT_SECONDS:
        raise ConfigurationError(f"IPC timeout must be finite and >= {MIN_TIMEOUT_SECONDS}s.")
    return timeout


# endregion


# region IPC Client
class IpcClient:
    """Synchronous fast-fail JSON-RPC 2.0 client talking to local `worktrackd`."""

    def __init__(self, endpoint: IpcEndpointConfig) -> None:
        """Initialize client with resolved `endpoint` parameters."""
        self._endpoint = endpoint
        self._id_counter = itertools.count(start=1)

    @property
    def endpoint(self) -> IpcEndpointConfig:
        """Return configured `IpcEndpointConfig`."""
        return self._endpoint

    def status(self) -> DaemonStatusSnapshot:
        """Query `worktrackd` `status` method and return `DaemonStatusSnapshot`."""
        return DaemonStatusSnapshot.from_dict(self.call_raw(METHOD_STATUS, {}))

    def clock_in(self, request: ClockInRequest) -> DaemonStatusSnapshot:
        """Invoke `clock_in` on `worktrackd` and return updated `DaemonStatusSnapshot`."""
        return DaemonStatusSnapshot.from_dict(self.call_raw(METHOD_CLOCK_IN, request.to_dict()))

    def clock_out(self, request: ClockOutRequest | None = None) -> DaemonStatusSnapshot:
        """Invoke `clock_out` on `worktrackd` and return updated `DaemonStatusSnapshot`."""
        payload = ClockOutRequest() if request is None else request
        return DaemonStatusSnapshot.from_dict(self.call_raw(METHOD_CLOCK_OUT, payload.to_dict()))

    def agenda_today(self) -> AgendaTodaySnapshot:
        """Query `agenda_today` on `worktrackd` and return `AgendaTodaySnapshot`."""
        return AgendaTodaySnapshot.from_dict(self.call_raw(METHOD_AGENDA_TODAY, {}))

    def outbox_summary(self) -> OutboxSummarySnapshot:
        """Query `outbox_summary` on `worktrackd` and return `OutboxSummarySnapshot`."""
        return OutboxSummarySnapshot.from_dict(self.call_raw(METHOD_OUTBOX_SUMMARY, {}))

    def call_raw(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        """Execute a single JSON-RPC 2.0 request/response round-trip over Unix or TCP."""
        request_id = next(self._id_counter)
        envelope: dict[str, object] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        if self._endpoint.auth_token:
            envelope["authorization"] = f"Bearer {self._endpoint.auth_token}"
        wire_request = encode_frame(envelope, framing=self._endpoint.framing)

        try:
            with self._connect_socket() as sock, sock.makefile("rb") as reader:
                sock.sendall(wire_request)
                framed_reply = read_frame(reader, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES)
        except OSError as exc:
            raise IpcTransportError(f"Failed to communicate with worktrackd: {exc}") from exc

        if framed_reply is None:
            raise IpcTransportError("worktrackd closed IPC socket before sending a response.")
        return _decode_jsonrpc_reply(framed_reply[0])

    def _connect_socket(self) -> socket.socket:
        """Open a Unix domain or TCP socket honouring `endpoint.timeout_seconds`."""
        if self._endpoint.transport == TRANSPORT_UNIX:
            sock_path = self._endpoint.socket_path
            if sock_path is None or not sock_path.exists():
                raise IpcTransportError("worktrackd Unix socket is offline.")
            if not hasattr(socket, "AF_UNIX"):
                raise IpcTransportError("AF_UNIX sockets are unavailable on this OS.")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._endpoint.timeout_seconds)
            try:
                sock.connect(str(sock_path))
            except OSError:
                sock.close()
                raise
            return sock

        if self._endpoint.tcp_port is None:
            raise IpcTransportError("TCP IPC endpoint is missing tcp_port.")
        return socket.create_connection(
            (self._endpoint.tcp_host, self._endpoint.tcp_port),
            timeout=self._endpoint.timeout_seconds,
        )


def _decode_jsonrpc_reply(raw_bytes: bytes) -> dict[str, object]:
    """Decode JSON-RPC 2.0 response bytes and unwrap `result` or raise `IpcRpcError`."""
    try:
        decoded = json.loads(raw_bytes.decode(FRAME_ENCODING))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcTransportError("Invalid JSON-RPC response from worktrackd.") from exc
    if not isinstance(decoded, Mapping):
        raise IpcTransportError("JSON-RPC response envelope must be an object.")

    error_obj = decoded.get("error")
    if isinstance(error_obj, Mapping):
        raw_code = error_obj.get("code", DEFAULT_UNKNOWN_RPC_ERROR_CODE)
        code = (
            raw_code
            if isinstance(raw_code, int) and not isinstance(raw_code, bool)
            else DEFAULT_UNKNOWN_RPC_ERROR_CODE
        )
        message = str(error_obj.get("message", "Unknown IPC error"))
        raise IpcRpcError(code=code, message=message, data=error_obj.get("data"))

    result = decoded.get("result")
    if not isinstance(result, Mapping):
        raise IpcTransportError("JSON-RPC response missing object 'result'.")
    return dict(result)


# endregion
