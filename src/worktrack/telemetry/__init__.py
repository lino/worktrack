"""Uhrenmaschine observability package for Worktrack (`worktrack.telemetry`)."""

from __future__ import annotations

from worktrack.telemetry.logging import (
    DEFAULT_EVENTS_FILENAME,
    ENV_FRITZ_TRACE_ID,
    StructuredEventLogger,
    resolve_trace_id,
)
from worktrack.telemetry.metrics import (
    ALLOWED_METRIC_LABELS,
    CANONICAL_METRIC_NAMES,
    CANONICAL_METRIC_SPECS,
    DEFAULT_MISSING_LABEL_VALUE,
    MetricSpec,
    TelemetryCardinalityError,
    TelemetryRecorder,
)
from worktrack.telemetry.server import (
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    MetricsHttpServer,
)

__all__ = [
    "ALLOWED_METRIC_LABELS",
    "CANONICAL_METRIC_NAMES",
    "CANONICAL_METRIC_SPECS",
    "DEFAULT_EVENTS_FILENAME",
    "DEFAULT_MISSING_LABEL_VALUE",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "ENV_FRITZ_TRACE_ID",
    "MetricSpec",
    "MetricsHttpServer",
    "StructuredEventLogger",
    "TelemetryCardinalityError",
    "TelemetryRecorder",
    "resolve_trace_id",
]
