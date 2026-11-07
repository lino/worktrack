"""Canonical Prometheus metric specifications, collector, and cardinality guards."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector

from worktrack.telemetry.persistence import (
    DEFAULT_HISTOGRAM_COUNT,
    HistogramSampleState,
    decode_label_values,
    read_telemetry_state,
)

# region Constants
ALLOWED_METRIC_LABELS: frozenset[str] = frozenset(
    {
        "mode",
        "sink",
        "collector",
        "provider",
        "cadence",
        "outcome",
        "subcommand",
        "method",
        "scheme",
        "grant",
        "route",
        "status_class",
        "source",
    }
)

DEFAULT_MISSING_LABEL_VALUE = "unspecified"
MAX_LABEL_VALUE_LENGTH = 64
DEFAULT_SERIES_LABEL_FLOOR = 20
CANONICAL_LABEL_VALUE_CEILING = 32
IPC_DURATION_BUCKETS: tuple[float, ...] = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
)
ROLLUP_DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
)
HTTP_DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)

UUID_VALUE_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
JIRA_KEY_VALUE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
HEX_HASH_VALUE_PATTERN = re.compile(r"^[0-9a-fA-F]{12,64}$")
# endregion


# region Exceptions
class TelemetryCardinalityError(ValueError):
    """Raised when a metric name, label key, value, or series budget is violated."""


# endregion


# region Models
@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Declarative specification for a canonical bounded Prometheus metric.

    Attributes:
        name: Canonical Prometheus metric name (`worktrack_*`).
        kind: Metric family type (`counter`, `gauge`, or `histogram`).
        description: Human-readable `# HELP` string.
        labels: Alphabetically sorted tuple of allowed label keys.
        buckets: Optional sorted upper-bound buckets for histograms.
    """

    name: str
    kind: str
    description: str
    labels: tuple[str, ...] = ()
    buckets: tuple[float, ...] = ()


# endregion


# region Registry
CANONICAL_METRIC_SPECS: dict[str, MetricSpec] = {
    spec.name: spec
    for spec in (
        MetricSpec(
            name="worktrack_ipc_request_duration_seconds",
            kind="histogram",
            description="Shell prompt and CLI IPC request round-trip latency.",
            labels=("method", "outcome"),
            buckets=IPC_DURATION_BUCKETS,
        ),
        MetricSpec(
            name="worktrack_intervals_recorded_total",
            kind="counter",
            description="Recorded local Intervals by capture mode.",
            labels=("mode",),
        ),
        MetricSpec(
            name="worktrack_rollup_compute_duration_seconds",
            kind="histogram",
            description="Local Rollup preview computation duration in seconds.",
            labels=("cadence", "outcome"),
            buckets=ROLLUP_DURATION_BUCKETS,
        ),
        MetricSpec(
            name="worktrack_outbox_flushes_total",
            kind="counter",
            description="Previewed Outbox flushes by cadence, outcome, and sink.",
            labels=("cadence", "outcome", "sink"),
        ),
        MetricSpec(
            name="worktrack_egress_bypass_blocked_total",
            kind="counter",
            description="Blocked attempts to write to external Sinks bypassing Outbox.",
            labels=("sink", "source"),
        ),
        MetricSpec(
            name="worktrack_oauth_broker_requests_total",
            kind="counter",
            description="Stateless OAuth Confidential Token-Exchange Broker requests.",
            labels=("grant", "outcome", "provider"),
        ),
        MetricSpec(
            name="worktrack_secret_provider_resolves_total",
            kind="counter",
            description="SecretProvider URI resolutions by outcome and scheme.",
            labels=("outcome", "scheme"),
        ),
        MetricSpec(
            name="worktrack_cli_commands_total",
            kind="counter",
            description="Worktrack CLI invocations by outcome and subcommand.",
            labels=("outcome", "subcommand"),
        ),
        MetricSpec(
            name="worktrack_attribution_queries_total",
            kind="counter",
            description="Entity Attribution graph queries executed on the local Node.",
        ),
        MetricSpec(
            name="worktrack_context_facts_asserted_total",
            kind="counter",
            description="Local Facet ContextFacts asserted on Intervals or dates.",
        ),
        MetricSpec(
            name="worktrack_ipc_active_connections",
            kind="gauge",
            description="Active concurrent client connections on worktrackd IPC socket.",
        ),
        MetricSpec(
            name="worktrack_agenda_items_scheduled_total",
            kind="counter",
            description="Agenda items and FocusBlocks scheduled in local planning.",
        ),
        MetricSpec(
            name="worktrack_source_syncs_total",
            kind="counter",
            description="Ambient Clue source collector sync runs by outcome and source.",
            labels=("outcome", "source"),
        ),
        MetricSpec(
            name="worktrack_http_requests_total",
            kind="counter",
            description="Web backend and broker HTTP requests by method, route, status.",
            labels=("method", "route", "status_class"),
        ),
        MetricSpec(
            name="worktrack_http_request_duration_seconds",
            kind="histogram",
            description="Web backend and broker HTTP request latency in seconds.",
            labels=("method", "route"),
            buckets=HTTP_DURATION_BUCKETS,
        ),
        MetricSpec(
            name="worktrack_fritz_worker_offload_ratio",
            kind="gauge",
            description="Share of Work Packages routed to L1..L3 worker tiers.",
            labels=("mode",),
        ),
        MetricSpec(
            name="worktrack_build_info",
            kind="gauge",
            description="Worktrack runtime topology build indicator.",
            labels=("mode",),
        ),
    )
}

CANONICAL_METRIC_NAMES: tuple[str, ...] = tuple(CANONICAL_METRIC_SPECS.keys())


class PersistedPrometheusCollector(Collector):
    """Custom `prometheus_client` collector rendering persisted telemetry state."""

    def __init__(self, state_path: Path) -> None:
        """Initialize collector bound to a persisted JSON state path."""
        self._state_path = state_path

    def collect(self) -> Iterator[Metric]:
        """Yield canonical Prometheus metric families hydrated from disk state."""
        state = read_telemetry_state(self._state_path)
        for spec in CANONICAL_METRIC_SPECS.values():
            if spec.kind == "counter":
                yield _build_counter_family(spec, state.counters.get(spec.name, {}))
            elif spec.kind == "gauge":
                yield _build_gauge_family(spec, state.gauges.get(spec.name, {}))
            else:
                yield _build_histogram_family(spec, state.histograms.get(spec.name, {}))


def _build_counter_family(spec: MetricSpec, series_map: Mapping[str, float]) -> CounterMetricFamily:
    """Construct a `CounterMetricFamily` from persisted counter totals."""
    family = CounterMetricFamily(spec.name, spec.description, labels=list(spec.labels))
    for encoded_labels, value in sorted(series_map.items()):
        family.add_metric(list(decode_label_values(encoded_labels)), value)
    return family


def _build_gauge_family(spec: MetricSpec, series_map: Mapping[str, float]) -> GaugeMetricFamily:
    """Construct a `GaugeMetricFamily` from persisted gauge values."""
    family = GaugeMetricFamily(spec.name, spec.description, labels=list(spec.labels))
    for encoded_labels, value in sorted(series_map.items()):
        family.add_metric(list(decode_label_values(encoded_labels)), value)
    return family


def _build_histogram_family(
    spec: MetricSpec, series_map: Mapping[str, HistogramSampleState]
) -> HistogramMetricFamily:
    """Construct a `HistogramMetricFamily` from persisted cumulative buckets."""
    family = HistogramMetricFamily(spec.name, spec.description, labels=list(spec.labels))
    for encoded_labels, sample in sorted(series_map.items()):
        buckets: list[tuple[str, float]] = [
            (str(bound), float(sample.buckets.get(str(bound), DEFAULT_HISTOGRAM_COUNT)))
            for bound in spec.buckets
        ]
        buckets.append(("+Inf", float(sample.count)))
        family.add_metric(
            list(decode_label_values(encoded_labels)),
            buckets=buckets,
            sum_value=sample.sum_value,
        )
    return family


# endregion


# region Validation
def validate_and_order_labels(spec: MetricSpec, labels: Mapping[str, str]) -> tuple[str, ...]:
    """Verify label keys and values satisfy allowlist and default omitted keys.

    Args:
        spec: Target metric specification.
        labels: Caller-supplied label dictionary (omitted `spec.labels` keys
            default to `DEFAULT_MISSING_LABEL_VALUE`).

    Returns:
        Ordered tuple of validated label values matching `spec.labels`.

    Raises:
        TelemetryCardinalityError: If any unknown/disallowed key or PII value is passed.
    """
    supplied_keys = set(labels.keys())
    unknown_keys = supplied_keys - ALLOWED_METRIC_LABELS
    if unknown_keys:
        joined = ", ".join(sorted(unknown_keys))
        raise TelemetryCardinalityError(f"Forbidden high-cardinality label key(s): {joined}.")
    unexpected_keys = supplied_keys - set(spec.labels)
    if unexpected_keys:
        joined = ", ".join(sorted(unexpected_keys))
        raise TelemetryCardinalityError(
            f"Unexpected label key(s) for {spec.name}: {joined} (allowed: {spec.labels})."
        )
    ordered_values: list[str] = []
    for key in spec.labels:
        val = labels.get(key, DEFAULT_MISSING_LABEL_VALUE)
        _validate_label_value(key, val)
        ordered_values.append(val)
    return tuple(ordered_values)


def _validate_label_value(label_key: str, value: str) -> None:
    """Reject empty, oversized, path, email, UUID, Jira key, or hash values."""
    stripped = value.strip()
    if not stripped or len(stripped) > MAX_LABEL_VALUE_LENGTH:
        raise TelemetryCardinalityError(
            f"Label '{label_key}' must be 1..{MAX_LABEL_VALUE_LENGTH} characters."
        )
    if any(ch in stripped for ch in ("@", "/", "\\")):
        raise TelemetryCardinalityError(
            f"Label '{label_key}' must not contain emails or filesystem paths."
        )
    if (
        UUID_VALUE_PATTERN.match(stripped)
        or JIRA_KEY_VALUE_PATTERN.match(stripped)
        or HEX_HASH_VALUE_PATTERN.match(stripped)
    ):
        raise TelemetryCardinalityError(
            f"Label '{label_key}' value '{stripped}' resembles high-cardinality ID."
        )


def enforce_series_budgets(
    spec: MetricSpec,
    existing_keys: set[str],
    candidate_key: str,
    candidate_tuple: tuple[str, ...],
    max_label_values: int,
    max_series_per_metric: int,
) -> None:
    """Enforce per-label distinct value and per-metric total series ceilings."""
    if candidate_key in existing_keys:
        return
    if len(existing_keys) + 1 > max_series_per_metric:
        raise TelemetryCardinalityError(
            f"Metric {spec.name} exceeded max_series_per_metric={max_series_per_metric}."
        )
    effective_max_labels = (
        max(max_label_values, CANONICAL_LABEL_VALUE_CEILING)
        if max_label_values >= DEFAULT_SERIES_LABEL_FLOOR
        else max_label_values
    )
    decoded_existing = [decode_label_values(key) for key in existing_keys]
    for idx, label_name in enumerate(spec.labels):
        distinct_vals = {row[idx] for row in decoded_existing}
        distinct_vals.add(candidate_tuple[idx])
        if len(distinct_vals) > effective_max_labels:
            msg = (
                f"Label '{label_name}' on {spec.name} exceeded "
                f"max_label_values={effective_max_labels}."
            )
            raise TelemetryCardinalityError(msg)


# endregion
