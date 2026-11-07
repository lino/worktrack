"""Atomic JSON state persistence for multi-run CLI Prometheus metrics."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

STATE_FILENAME = "metrics_state.json"
LOCK_FILENAME = "metrics_state.lock"
JSON_INDENT_SPACES: int = 2
DEFAULT_HISTOGRAM_COUNT: int = 0
DEFAULT_HISTOGRAM_SUM: float = 0.0


@dataclass(slots=True)
class HistogramSampleState:
    """Mutable bucket, count, and sum accumulator for one histogram series.

    Attributes:
        buckets: Cumulative upper-bound counts keyed by string bound.
        count: Total number of observed samples.
        sum_value: Sum of observed floating-point sample values.
    """

    buckets: dict[str, int] = field(default_factory=dict)
    count: int = DEFAULT_HISTOGRAM_COUNT
    sum_value: float = DEFAULT_HISTOGRAM_SUM


@dataclass(slots=True)
class PersistedMetricsState:
    """In-memory representation of persisted counters, gauges, and histograms.

    Attributes:
        counters: Mapping of metric name to serialized label tuple -> total.
        gauges: Mapping of metric name to serialized label tuple -> value.
        histograms: Mapping of metric name to serialized label tuple -> state.
    """

    counters: dict[str, dict[str, float]] = field(default_factory=dict)
    gauges: dict[str, dict[str, float]] = field(default_factory=dict)
    histograms: dict[str, dict[str, HistogramSampleState]] = field(default_factory=dict)


def encode_label_values(values: tuple[str, ...]) -> str:
    """Serialize an ordered tuple of label values into a stable JSON map key."""
    return json.dumps(list(values), separators=(",", ":"))


def decode_label_values(encoded: str) -> tuple[str, ...]:
    """Deserialize a JSON map key back into a tuple of string label values."""
    raw = json.loads(encoded)
    return tuple(str(item) for item in raw)


@contextlib.contextmanager
def locked_telemetry_state(telemetry_dir: Path) -> Iterator[PersistedMetricsState]:
    """Acquire exclusive telemetry lock, load state, yield, and save atomically."""
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    lock_path = telemetry_dir / LOCK_FILENAME
    state_path = telemetry_dir / STATE_FILENAME

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            state = read_telemetry_state(state_path)
            yield state
            _write_telemetry_state_atomic(state_path, state)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def read_telemetry_state(state_path: Path) -> PersistedMetricsState:
    """Read persisted metric state from disk, returning empty state if absent."""
    if not state_path.exists():
        return PersistedMetricsState()
    raw_payload = json.loads(state_path.read_text(encoding="utf-8"))
    counters = {
        metric: {k: float(v) for k, v in series.items()}
        for metric, series in raw_payload.get("counters", {}).items()
    }
    gauges = {
        metric: {k: float(v) for k, v in series.items()}
        for metric, series in raw_payload.get("gauges", {}).items()
    }
    histograms: dict[str, dict[str, HistogramSampleState]] = {}
    for metric, series in raw_payload.get("histograms", {}).items():
        histograms[metric] = {
            k: HistogramSampleState(
                buckets={bk: int(bv) for bk, bv in sample.get("buckets", {}).items()},
                count=int(sample.get("count", DEFAULT_HISTOGRAM_COUNT)),
                sum_value=float(sample.get("sum_value", DEFAULT_HISTOGRAM_SUM)),
            )
            for k, sample in series.items()
        }
    return PersistedMetricsState(counters=counters, gauges=gauges, histograms=histograms)


def _write_telemetry_state_atomic(state_path: Path, state: PersistedMetricsState) -> None:
    """Serialize and atomically replace `metrics_state.json` via temp file."""
    serializable = {
        "counters": state.counters,
        "gauges": state.gauges,
        "histograms": {
            metric: {
                key: {
                    "buckets": sample.buckets,
                    "count": sample.count,
                    "sum_value": sample.sum_value,
                }
                for key, sample in series.items()
            }
            for metric, series in state.histograms.items()
        },
    }
    encoded = json.dumps(serializable, indent=JSON_INDENT_SPACES, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=state_path.parent, prefix=".metrics_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, state_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
