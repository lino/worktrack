"""Multi-run Prometheus telemetry recorder backed by atomic local state."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest

from worktrack.config import (
    DEFAULT_DEPLOYMENT_MODE,
    DEFAULT_MAX_LABEL_VALUES,
    DEFAULT_MAX_SERIES_PER_METRIC,
    WorktrackSettings,
)
from worktrack.telemetry.catalog import (
    ALLOWED_METRIC_LABELS,
    CANONICAL_METRIC_NAMES,
    CANONICAL_METRIC_SPECS,
    DEFAULT_MISSING_LABEL_VALUE,
    MetricSpec,
    PersistedPrometheusCollector,
    TelemetryCardinalityError,
    enforce_series_budgets,
    validate_and_order_labels,
)
from worktrack.telemetry.persistence import (
    DEFAULT_HISTOGRAM_COUNT,
    DEFAULT_HISTOGRAM_SUM,
    STATE_FILENAME,
    HistogramSampleState,
    PersistedMetricsState,
    encode_label_values,
    locked_telemetry_state,
)

BUILD_INFO_PRESENT_VALUE: float = 1.0
DEFAULT_COUNTER_INCREMENT: float = 1.0
MIN_NON_NEGATIVE_VALUE: float = 0.0
HISTOGRAM_SAMPLE_INCREMENT: int = 1

__all__ = [
    "ALLOWED_METRIC_LABELS",
    "CANONICAL_METRIC_NAMES",
    "CANONICAL_METRIC_SPECS",
    "DEFAULT_MISSING_LABEL_VALUE",
    "MetricSpec",
    "TelemetryCardinalityError",
    "TelemetryRecorder",
]


class TelemetryRecorder:
    """Bounded-cardinality Prometheus recorder with atomic multi-run persistence."""

    def __init__(
        self,
        telemetry_dir: Path,
        *,
        mode: str = DEFAULT_DEPLOYMENT_MODE,
        max_label_values: int = DEFAULT_MAX_LABEL_VALUES,
        max_series_per_metric: int = DEFAULT_MAX_SERIES_PER_METRIC,
    ) -> None:
        """Initialize telemetry recorder and seed `worktrack_build_info`.

        Args:
            telemetry_dir: Directory storing atomic metric state and lockfiles.
            mode: Active topology mode (`node` or `hub`).
            max_label_values: Maximum distinct values per label key.
            max_series_per_metric: Maximum distinct label series per metric.
        """
        self._telemetry_dir = telemetry_dir
        self._mode = mode
        self._max_label_values = max_label_values
        self._max_series_per_metric = max_series_per_metric
        self._registry = CollectorRegistry(auto_describe=False)
        self._registry.register(PersistedPrometheusCollector(self._telemetry_dir / STATE_FILENAME))
        self.set_gauge("worktrack_build_info", {"mode": self._mode}, BUILD_INFO_PRESENT_VALUE)

    @classmethod
    def from_settings(cls, settings: WorktrackSettings) -> TelemetryRecorder:
        """Construct a `TelemetryRecorder` from `WorktrackSettings`."""
        return cls(
            telemetry_dir=settings.telemetry_dir,
            mode=settings.mode,
            max_label_values=settings.max_label_values,
            max_series_per_metric=settings.max_series_per_metric,
        )

    @property
    def mode(self) -> str:
        """Return active runtime topology mode (`node` or `hub`)."""
        return self._mode

    def increment_counter(
        self,
        metric_name: str,
        labels: Mapping[str, str] | None = None,
        amount: float = DEFAULT_COUNTER_INCREMENT,
    ) -> None:
        """Increment a canonical counter metric and persist state atomically."""
        spec = _require_spec(metric_name, expected_kind="counter")
        if amount < MIN_NON_NEGATIVE_VALUE or not math.isfinite(amount):
            raise TelemetryCardinalityError("Counter increment must be finite and >= 0.")
        ordered = validate_and_order_labels(spec, {} if labels is None else labels)
        encoded = encode_label_values(ordered)
        with locked_telemetry_state(self._telemetry_dir) as state:
            series = state.counters.setdefault(spec.name, {})
            enforce_series_budgets(
                spec,
                set(series.keys()),
                encoded,
                ordered,
                self._max_label_values,
                self._max_series_per_metric,
            )
            series[encoded] = series.get(encoded, MIN_NON_NEGATIVE_VALUE) + amount

    def set_gauge(
        self,
        metric_name: str,
        labels: Mapping[str, str] | None = None,
        value: float = MIN_NON_NEGATIVE_VALUE,
    ) -> None:
        """Set a canonical gauge metric value and persist state atomically."""
        spec = _require_spec(metric_name, expected_kind="gauge")
        if not math.isfinite(value):
            raise TelemetryCardinalityError("Gauge value must be finite.")
        ordered = validate_and_order_labels(spec, {} if labels is None else labels)
        encoded = encode_label_values(ordered)
        with locked_telemetry_state(self._telemetry_dir) as state:
            series = state.gauges.setdefault(spec.name, {})
            enforce_series_budgets(
                spec,
                set(series.keys()),
                encoded,
                ordered,
                self._max_label_values,
                self._max_series_per_metric,
            )
            series[encoded] = value

    def observe_histogram(
        self,
        metric_name: str,
        labels: Mapping[str, str] | None = None,
        value: float = MIN_NON_NEGATIVE_VALUE,
    ) -> None:
        """Record a non-negative duration/observation into a canonical histogram."""
        spec = _require_spec(metric_name, expected_kind="histogram")
        if value < MIN_NON_NEGATIVE_VALUE or not math.isfinite(value):
            raise TelemetryCardinalityError("Histogram observation must be finite >= 0.")
        ordered = validate_and_order_labels(spec, {} if labels is None else labels)
        encoded = encode_label_values(ordered)
        with locked_telemetry_state(self._telemetry_dir) as state:
            _record_histogram_sample(
                state,
                spec,
                encoded,
                ordered,
                value,
                self._max_label_values,
                self._max_series_per_metric,
            )

    def record_cli_command(self, subcommand: str, outcome: str = "ok") -> None:
        """Record a `wt` CLI command execution outcome."""
        self.increment_counter(
            "worktrack_cli_commands_total",
            {"subcommand": subcommand, "outcome": outcome},
        )

    def record_interval(self, mode: str) -> None:
        """Record a persisted local `Interval` by capture `mode`."""
        self.increment_counter("worktrack_intervals_recorded_total", {"mode": mode})

    def observe_ipc_duration(self, method: str, outcome: str, duration_seconds: float) -> None:
        """Observe a `worktrackd` IPC request round-trip latency in seconds."""
        self.observe_histogram(
            "worktrack_ipc_request_duration_seconds",
            {"method": method, "outcome": outcome},
            duration_seconds,
        )

    def observe_rollup_duration(self, cadence: str, outcome: str, duration_seconds: float) -> None:
        """Observe local `Rollup` computation duration in seconds."""
        self.observe_histogram(
            "worktrack_rollup_compute_duration_seconds",
            {"cadence": cadence, "outcome": outcome},
            duration_seconds,
        )

    def record_outbox_flush(self, sink: str, cadence: str, outcome: str) -> None:
        """Record an `Outbox` flush attempt to an external `Sink`."""
        self.increment_counter(
            "worktrack_outbox_flushes_total",
            {"sink": sink, "cadence": cadence, "outcome": outcome},
        )

    def record_egress_bypass_blocked(self, sink: str, source: str) -> None:
        """Record a blocked attempt to write to a `Sink` bypassing `Outbox`."""
        self.increment_counter(
            "worktrack_egress_bypass_blocked_total",
            {"sink": sink, "source": source},
        )

    def record_oauth_broker_request(self, provider: str, grant: str, outcome: str) -> None:
        """Record a stateless OAuth Confidential Token-Exchange Broker request."""
        self.increment_counter(
            "worktrack_oauth_broker_requests_total",
            {"provider": provider, "grant": grant, "outcome": outcome},
        )

    def record_secret_resolve(self, scheme: str, outcome: str) -> None:
        """Record a `SecretProvider` URI resolution attempt."""
        self.increment_counter(
            "worktrack_secret_provider_resolves_total",
            {"scheme": scheme, "outcome": outcome},
        )

    def render_metrics(self) -> bytes:
        """Render Prometheus text exposition bytes from persisted state."""
        return generate_latest(self._registry)

    def render_metrics_text(self) -> str:
        """Render Prometheus text exposition as a decoded UTF-8 string."""
        return self.render_metrics().decode("utf-8")


def _require_spec(metric_name: str, *, expected_kind: str) -> MetricSpec:
    """Look up a canonical `MetricSpec` and verify its metric family kind."""
    spec = CANONICAL_METRIC_SPECS.get(metric_name)
    if spec is None:
        raise TelemetryCardinalityError(f"Unknown Worktrack metric: '{metric_name}'.")
    if spec.kind != expected_kind:
        raise TelemetryCardinalityError(
            f"Metric '{metric_name}' is a {spec.kind}, not {expected_kind}."
        )
    return spec


def _record_histogram_sample(
    state: PersistedMetricsState,
    spec: MetricSpec,
    encoded: str,
    ordered: tuple[str, ...],
    value: float,
    max_label_values: int,
    max_series_per_metric: int,
) -> None:
    """Update cumulative bucket counts, count, and sum for a histogram series."""
    series = state.histograms.setdefault(spec.name, {})
    enforce_series_budgets(
        spec,
        set(series.keys()),
        encoded,
        ordered,
        max_label_values,
        max_series_per_metric,
    )
    sample = series.get(encoded)
    if sample is None:
        sample = HistogramSampleState(
            buckets={str(bound): DEFAULT_HISTOGRAM_COUNT for bound in spec.buckets},
            count=DEFAULT_HISTOGRAM_COUNT,
            sum_value=DEFAULT_HISTOGRAM_SUM,
        )
        series[encoded] = sample
    for bound in spec.buckets:
        key = str(bound)
        sample.buckets.setdefault(key, DEFAULT_HISTOGRAM_COUNT)
        if value <= bound:
            sample.buckets[key] += HISTOGRAM_SAMPLE_INCREMENT
    sample.count += HISTOGRAM_SAMPLE_INCREMENT
    sample.sum_value += value
