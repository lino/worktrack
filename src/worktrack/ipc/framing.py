"""Newline-delimited and length-prefixed UTF-8 JSON-RPC 2.0 wire framing helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import BinaryIO

from worktrack.ipc.models import IpcTransportError

# region Constants
JSONRPC_VERSION = "2.0"
FRAME_ENCODING = "utf-8"
HEADER_ENCODING = "ascii"

FRAMING_NEWLINE = "newline"
FRAMING_LENGTH_PREFIXED = "length_prefixed"
ALLOWED_FRAMING_MODES: frozenset[str] = frozenset({FRAMING_NEWLINE, FRAMING_LENGTH_PREFIXED})

DEFAULT_MAX_FRAME_BYTES = 1_048_576
MIN_FRAME_BYTES = 1
MAX_NEWLINE_DELIMITER_BYTES = 2
MAX_HEADER_LINES = 16
CONTENT_LENGTH_HEADER_PREFIX = "content-length:"

METHOD_STATUS = "status"
METHOD_CLOCK_IN = "clock_in"
METHOD_CLOCK_OUT = "clock_out"
METHOD_AGENDA_TODAY = "agenda_today"
METHOD_OUTBOX_SUMMARY = "outbox_summary"
UNKNOWN_METHOD_LABEL = "unknown"

CANONICAL_IPC_METHODS: frozenset[str] = frozenset(
    {
        METHOD_STATUS,
        METHOD_CLOCK_IN,
        METHOD_CLOCK_OUT,
        METHOD_AGENDA_TODAY,
        METHOD_OUTBOX_SUMMARY,
    }
)

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_UNAUTHORIZED = "unauthorized"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_AUTH_ERROR = -32001
# endregion


# region Framing
def normalize_method_label(method_name: object) -> str:
    """Map an incoming RPC method name to a bounded Prometheus metric `method` label."""
    if isinstance(method_name, str) and method_name in CANONICAL_IPC_METHODS:
        return method_name
    return UNKNOWN_METHOD_LABEL


def encode_frame(
    payload: Mapping[str, object],
    framing: str = FRAMING_NEWLINE,
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> bytes:
    """Encode a JSON-RPC 2.0 mapping as newline-delimited or `Content-Length` framed bytes.

    Args:
        payload: JSON-serializable request or response envelope.
        framing: Wire framing mode (`newline` or `length_prefixed`).
        max_frame_bytes: Maximum permitted UTF-8 JSON body length in bytes.

    Returns:
        UTF-8 encoded wire bytes ready for `socket.sendall`.

    Raises:
        IpcTransportError: If `framing` is unknown or payload exceeds `max_frame_bytes`.
    """
    if framing not in ALLOWED_FRAMING_MODES:
        raise IpcTransportError(f"Unsupported IPC framing mode: '{framing}'.")
    body = json.dumps(payload, separators=(",", ":")).encode(FRAME_ENCODING)
    if len(body) > max_frame_bytes:
        raise IpcTransportError(f"Encoded IPC frame exceeds {max_frame_bytes} bytes.")
    if framing == FRAMING_LENGTH_PREFIXED:
        header = f"Content-Length: {len(body)}\r\n\r\n".encode(HEADER_ENCODING)
        return header + body
    return body + b"\n"


def read_frame(
    stream: BinaryIO,
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> tuple[bytes, str] | None:
    """Read one auto-detected newline or `Content-Length` frame from `stream`.

    Returns:
        Tuple of `(raw_json_bytes, detected_framing)` or `None` on clean EOF.
    """
    max_line_read = max_frame_bytes + MAX_NEWLINE_DELIMITER_BYTES
    while True:
        first_line = stream.readline(max_line_read + 1)
        if not first_line:
            return None
        stripped = first_line.rstrip(b"\r\n")
        if len(first_line) > max_line_read or len(stripped) > max_frame_bytes:
            raise IpcTransportError(f"IPC frame line exceeded {max_frame_bytes} bytes.")
        if stripped:
            break

    lower_ascii = stripped.lower()
    if lower_ascii.startswith(CONTENT_LENGTH_HEADER_PREFIX.encode(HEADER_ENCODING)):
        body = _read_length_prefixed_body(stream, stripped, max_frame_bytes=max_frame_bytes)
        return body, FRAMING_LENGTH_PREFIXED
    return stripped, FRAMING_NEWLINE


def _read_length_prefixed_body(
    stream: BinaryIO,
    header_line: bytes,
    *,
    max_frame_bytes: int,
) -> bytes:
    """Parse `Content-Length` header, consume blank separator, and read exact body bytes."""
    try:
        header_text = header_line.decode(HEADER_ENCODING)
        _, raw_length = header_text.split(":", maxsplit=1)
        content_length = int(raw_length.strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise IpcTransportError("Malformed Content-Length header in IPC frame.") from exc

    if not (MIN_FRAME_BYTES <= content_length <= max_frame_bytes):
        raise IpcTransportError(f"Content-Length {content_length} out of bounds.")

    for _ in range(MAX_HEADER_LINES):
        sep_line = stream.readline(max_frame_bytes + 1)
        if not sep_line:
            raise IpcTransportError("Unexpected EOF while reading IPC frame headers.")
        if not sep_line.rstrip(b"\r\n"):
            break
    else:
        raise IpcTransportError("Exceeded maximum IPC header lines before blank separator.")

    body = stream.read(content_length)
    if len(body) != content_length:
        raise IpcTransportError("Truncated length-prefixed IPC frame body.")
    return body


def build_result_response(request_id: object, result: Mapping[str, object]) -> dict[str, object]:
    """Build a canonical JSON-RPC 2.0 success response dictionary."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "result": dict(result),
    }


def build_error_response(
    request_id: object,
    *,
    code: int,
    message: str,
    data: object | None = None,
) -> dict[str, object]:
    """Build a canonical JSON-RPC 2.0 error response dictionary."""
    error_obj: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error_obj["data"] = data
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": error_obj,
    }


# endregion
