"""JSON-RPC 2.0 request parsing, bearer authentication, and `StatusProvider` dispatch."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import socket
import stat
from collections.abc import Mapping
from pathlib import Path

from worktrack.config import MAX_TCP_PORT
from worktrack.ipc.framing import (
    FRAME_ENCODING,
    JSONRPC_AUTH_ERROR,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    JSONRPC_VERSION,
    METHOD_AGENDA_TODAY,
    METHOD_CLOCK_IN,
    METHOD_CLOCK_OUT,
    METHOD_OUTBOX_SUMMARY,
    METHOD_STATUS,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_UNAUTHORIZED,
    UNKNOWN_METHOD_LABEL,
    build_error_response,
    build_result_response,
    normalize_method_label,
)
from worktrack.ipc.models import (
    ClockInRequest,
    ClockOutRequest,
    IpcApplicationError,
    IpcAuthenticationError,
    IpcTransportError,
    StatusProvider,
)

# region Constants
LOOPBACK_IPV4 = "127.0.0.1"
LOOPBACK_IPV6 = "::1"
LOOPBACK_HOSTNAME = "localhost"
MIN_BIND_TCP_PORT = 0
RUNTIME_DIR_MODE = 0o700
STALE_SOCKET_PROBE_TIMEOUT = 0.05
BEARER_SCHEME_PREFIX = "bearer "
_LOGGER = logging.getLogger(__name__)
# endregion


# region Validation
def validate_tcp_bind_address(host: str, *, allow_non_loopback: bool = False) -> str:
    """Verify TCP bind address is strictly loopback unless explicitly overridden."""
    normalized = host.strip()
    if not normalized:
        raise IpcTransportError("TCP bind host must be non-empty.")
    if allow_non_loopback or normalized.lower() == LOOPBACK_HOSTNAME:
        return normalized
    try:
        parsed_ip = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise IpcTransportError(
            f"Refusing non-loopback TCP IPC host '{normalized}' without explicit override."
        ) from exc
    if not parsed_ip.is_loopback:
        raise IpcTransportError(
            f"Refusing to bind worktrackd TCP IPC on non-loopback IP '{normalized}'."
        )
    return normalized


def validate_tcp_port(tcp_port: int) -> int:
    """Validate TCP bind port is a non-boolean integer within `0..65535`."""
    if (
        isinstance(tcp_port, bool)
        or not isinstance(tcp_port, int)
        or not (MIN_BIND_TCP_PORT <= tcp_port <= MAX_TCP_PORT)
    ):
        raise IpcTransportError(
            f"TCP IPC port must be an integer between {MIN_BIND_TCP_PORT} and {MAX_TCP_PORT}."
        )
    return tcp_port


def resolve_tcp_socket_target(
    host: str, port: int
) -> tuple[socket.AddressFamily, tuple[object, ...]]:
    """Resolve IPv4 or IPv6 stream socket family and bind sockaddr tuple."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise IpcTransportError(f"Cannot resolve TCP IPC bind target {host}:{port}: {exc}") from exc
    for family, _, _, _, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6):
            return family, sockaddr
    raise IpcTransportError(f"No IPv4/IPv6 stream address available for {host}:{port}.")


def validate_tcp_auth_token(auth_token: str | None) -> str:
    """Require a non-empty bearer token whenever TCP transport is configured."""
    if auth_token is None or not auth_token.strip():
        raise IpcAuthenticationError("Loopback TCP IPC requires a non-empty bearer auth_token.")
    return auth_token.strip()


def verify_bearer_token(expected_token: str, candidate_token: str | None) -> bool:
    """Compare bearer tokens in constant time via `hmac.compare_digest`."""
    if not candidate_token:
        return False
    return hmac.compare_digest(
        expected_token.encode(FRAME_ENCODING),
        candidate_token.encode(FRAME_ENCODING),
    )


def extract_bearer_token(envelope: Mapping[str, object]) -> str | None:
    """Extract bearer token from `authorization` or `auth_token` JSON-RPC envelope fields."""
    raw_auth = envelope.get("authorization")
    if isinstance(raw_auth, str):
        stripped = raw_auth.strip()
        if stripped.lower().startswith(BEARER_SCHEME_PREFIX):
            token = stripped[len(BEARER_SCHEME_PREFIX) :].strip()
            return token if token else None
    raw_token = envelope.get("auth_token")
    if isinstance(raw_token, str) and raw_token.strip():
        return raw_token.strip()
    return None


def prepare_unix_socket_path(socket_path: Path) -> None:
    """Create parent runtime directory and clean up stale socket files safely."""
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=RUNTIME_DIR_MODE)
    if not socket_path.exists() and not socket_path.is_symlink():
        return
    if socket_path.is_dir():
        raise IpcTransportError(f"IPC socket path '{socket_path}' is a directory.")

    st_mode = socket_path.lstat().st_mode
    if stat.S_ISSOCK(st_mode) and _is_unix_socket_alive(socket_path):
        raise IpcTransportError(f"Another worktrackd instance is active at '{socket_path}'.")
    socket_path.unlink(missing_ok=True)


def _is_unix_socket_alive(socket_path: Path) -> bool:
    """Probe an existing Unix domain socket to check whether a live listener responds."""
    if not hasattr(socket, "AF_UNIX"):
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(STALE_SOCKET_PROBE_TIMEOUT)
        try:
            probe.connect(str(socket_path))
        except OSError:
            return False
        return True


# endregion


# region Framing
class _JsonRpcFrameError(Exception):
    """Internal helper carrying JSON-RPC error code and parsed request ID."""

    def __init__(self, code: int, message: str, request_id: object = None) -> None:
        """Store JSON-RPC `code`, `message`, and optional `request_id`."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


def dispatch_jsonrpc_frame(
    raw_bytes: bytes,
    *,
    provider: StatusProvider,
    require_tcp_auth: bool,
    expected_auth_token: str | None,
) -> tuple[dict[str, object], str, str]:
    """Parse, authenticate, and dispatch a JSON-RPC 2.0 frame.

    Returns:
        Tuple of `(response_dict, bounded_method_label, outcome_label)`.
    """
    try:
        envelope, request_id = _parse_jsonrpc_envelope(raw_bytes)
    except _JsonRpcFrameError as err:
        resp = build_error_response(err.request_id, code=err.code, message=err.message)
        return resp, UNKNOWN_METHOD_LABEL, OUTCOME_ERROR

    raw_method = envelope.get("method")
    method_label = normalize_method_label(raw_method)
    if require_tcp_auth and not verify_bearer_token(
        expected_auth_token or "", extract_bearer_token(envelope)
    ):
        resp = build_error_response(
            request_id,
            code=JSONRPC_AUTH_ERROR,
            message="Unauthorized: missing or invalid bearer token.",
        )
        return resp, method_label, OUTCOME_UNAUTHORIZED

    response, outcome = _dispatch_authenticated(provider, raw_method, envelope, request_id)
    return response, method_label, outcome


def _parse_jsonrpc_envelope(raw_bytes: bytes) -> tuple[Mapping[str, object], object]:
    """Decode UTF-8 JSON bytes and validate basic JSON-RPC 2.0 object structure."""
    try:
        decoded = json.loads(raw_bytes.decode(FRAME_ENCODING))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _JsonRpcFrameError(JSONRPC_PARSE_ERROR, "Invalid JSON payload.") from exc
    if not isinstance(decoded, Mapping):
        raise _JsonRpcFrameError(JSONRPC_INVALID_REQUEST, "JSON-RPC request must be an object.")
    request_id = decoded.get("id")
    if decoded.get("jsonrpc") != JSONRPC_VERSION:
        raise _JsonRpcFrameError(
            JSONRPC_INVALID_REQUEST, "jsonrpc version must equal '2.0'.", request_id
        )
    return decoded, request_id


def _dispatch_authenticated(
    provider: StatusProvider,
    raw_method: object,
    envelope: Mapping[str, object],
    request_id: object,
) -> tuple[dict[str, object], str]:
    """Validate `params` and invoke the matching `StatusProvider` method."""
    if not isinstance(raw_method, str):
        return (
            build_error_response(
                request_id, code=JSONRPC_INVALID_REQUEST, message="Method must be a string."
            ),
            OUTCOME_ERROR,
        )
    raw_params = envelope.get("params", {})
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, Mapping):
        return (
            build_error_response(
                request_id, code=JSONRPC_INVALID_PARAMS, message="Params must be an object."
            ),
            OUTCOME_ERROR,
        )
    try:
        result = _invoke_provider_method(provider, raw_method, raw_params)
    except _JsonRpcFrameError as rpc_err:
        return (
            build_error_response(request_id, code=rpc_err.code, message=rpc_err.message),
            OUTCOME_ERROR,
        )
    except ValueError as val_err:
        return (
            build_error_response(request_id, code=JSONRPC_INVALID_PARAMS, message=str(val_err)),
            OUTCOME_ERROR,
        )
    except IpcApplicationError as app_err:
        return (
            build_error_response(request_id, code=app_err.code, message=app_err.message),
            OUTCOME_ERROR,
        )
    except Exception:
        _LOGGER.exception("Unhandled error executing StatusProvider method '%s'", raw_method)
        return (
            build_error_response(
                request_id, code=JSONRPC_INTERNAL_ERROR, message="Internal worktrackd IPC error."
            ),
            OUTCOME_ERROR,
        )
    return build_result_response(request_id, result), OUTCOME_OK


def _invoke_provider_method(
    provider: StatusProvider,
    method_name: str,
    params: Mapping[str, object],
) -> dict[str, object]:
    """Dispatch one of the 5 canonical IPC methods onto `provider`."""
    if method_name == METHOD_STATUS:
        return provider.status().to_dict()
    if method_name == METHOD_CLOCK_IN:
        return provider.clock_in(ClockInRequest.from_dict(params)).to_dict()
    if method_name == METHOD_CLOCK_OUT:
        return provider.clock_out(ClockOutRequest.from_dict(params)).to_dict()
    if method_name == METHOD_AGENDA_TODAY:
        return provider.agenda_today().to_dict()
    if method_name == METHOD_OUTBOX_SUMMARY:
        return provider.outbox_summary().to_dict()
    raise _JsonRpcFrameError(JSONRPC_METHOD_NOT_FOUND, f"Unknown IPC method: '{method_name}'.")


# endregion
