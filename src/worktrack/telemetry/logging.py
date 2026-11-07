"""Structured JSONL event logging with W3C `FRITZ_TRACE_ID` propagation."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from worktrack.config import APP_SLUG, DEFAULT_LOG_LEVEL

ENV_FRITZ_TRACE_ID = "FRITZ_TRACE_ID"
ENV_W3C_TRACEPARENT = "TRACEPARENT"
DEFAULT_EVENTS_FILENAME = "events.jsonl"
TRACE_ID_BYTE_LEN = 16
W3C_TRACEPARENT_PARTS = 4
W3C_TRACE_ID_INDEX = 1


def resolve_trace_id(
    environ: Mapping[str, str] | None = None,
    explicit_trace_id: str | None = None,
) -> str:
    """Resolve `FRITZ_TRACE_ID` or W3C `TRACEPARENT`, or mint a 32-hex trace ID."""
    if explicit_trace_id and explicit_trace_id.strip():
        return explicit_trace_id.strip()

    env = os.environ if environ is None else environ
    fritz_trace = env.get(ENV_FRITZ_TRACE_ID, "").strip()
    if fritz_trace:
        return _extract_trace_from_header(fritz_trace)

    traceparent = env.get(ENV_W3C_TRACEPARENT, "").strip()
    if traceparent:
        return _extract_trace_from_header(traceparent)

    return secrets.token_hex(TRACE_ID_BYTE_LEN)


def _extract_trace_from_header(raw_value: str) -> str:
    """Extract the 32-char trace-id from a W3C `traceparent` or return raw ID."""
    parts = raw_value.split("-")
    if len(parts) == W3C_TRACEPARENT_PARTS and parts[W3C_TRACE_ID_INDEX]:
        return parts[W3C_TRACE_ID_INDEX]
    return raw_value


class StructuredEventLogger:
    """Append-only structured JSONL logger correlating high-cardinality context."""

    def __init__(
        self,
        telemetry_dir: Path,
        *,
        events_filename: str = DEFAULT_EVENTS_FILENAME,
        namespace: str = APP_SLUG,
    ) -> None:
        """Initialize JSONL logger writing under `telemetry_dir`."""
        self._telemetry_dir = telemetry_dir
        self._events_path = telemetry_dir / events_filename
        self._namespace = namespace

    @property
    def events_path(self) -> Path:
        """Return path to the structured JSONL event file."""
        return self._events_path

    def emit(
        self,
        event: str,
        *,
        level: str = DEFAULT_LOG_LEVEL,
        trace_id: str | None = None,
        environ: Mapping[str, str] | None = None,
        **attributes: object,
    ) -> dict[str, object]:
        """Emit a single structured JSONL event correlated with `FRITZ_TRACE_ID`."""
        resolved_trace = resolve_trace_id(environ=environ, explicit_trace_id=trace_id)
        record: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level.upper(),
            "namespace": self._namespace,
            "event": event,
            "trace_id": resolved_trace,
            "attributes": dict(attributes),
        }
        self._telemetry_dir.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(record, sort_keys=True)
        with self._events_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
        return record
