"""Benchmark test (`BENCH-SPEC-WT`) for `wt_rollup_preview_1k` (`p95 <= 0.050s`)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest_benchmark.fixture import BenchmarkFixture

from worktrack.core import ACTIVITY_KIND_STANDING, ACTIVITY_KIND_TARGETED, Ledger
from worktrack.outbox import (
    FLUSH_CADENCE_EOD,
    GROUPING_WINDOW_DAY,
    QUANTUM_FIFTEEN_MINUTES,
    ROUNDING_MODE_NEAREST,
    OutboxStore,
    RollupEngine,
    RollupPolicy,
    RollupPreviewResult,
)

# region Constants
CORPUS_INTERVAL_COUNT = 1_000
CORPUS_ACTIVITY_COUNT = 120
STANDING_STRIDE = 6
CHAOTIC_STRIDE = 4
INTERVAL_DURATION_MINUTES = 30
INTERVAL_GAP_MINUTES = 5
P95_QUANTILE = 0.95
ROLLUP_PREVIEW_1K_P95_BUDGET_SECONDS = 0.050
CORPUS_EPOCH = datetime(2026, 8, 1, 8, 0, 0, tzinfo=UTC)
# endregion


# region Fixtures
def _percentile(sorted_samples: Sequence[float], quantile: float) -> float:
    """Compute nearest-rank percentile from a non-empty sorted sample list."""
    idx = min(len(sorted_samples) - 1, max(0, math.ceil(quantile * len(sorted_samples)) - 1))
    return float(sorted_samples[idx])


def _seed_ledger_1k_for_rollup(db_path: Path) -> tuple[Ledger, OutboxStore, dict[str, str]]:
    """Seed canonical `ledger-1k` SQLite fixture (1,000 intervals across 120 activities)."""
    ledger = Ledger(db_path)
    outbox_store = OutboxStore(db_path)
    activity_ids: list[str] = []
    standing_mappings: dict[str, str] = {}

    for idx in range(CORPUS_ACTIVITY_COUNT):
        is_standing = idx % STANDING_STRIDE == 0
        kind = ACTIVITY_KIND_STANDING if is_standing else ACTIVITY_KIND_TARGETED
        name = f"Standing Stream {idx}" if is_standing else f"Targeted Issue {idx}"
        ext_ref = None if is_standing else f"WT-{idx + 1}"
        act = ledger.create_activity(name, kind=kind, external_ref=ext_ref)
        activity_ids.append(act.id)
        if is_standing:
            standing_mappings[name] = f"OPS-{idx + 1}"

    duration = timedelta(minutes=INTERVAL_DURATION_MINUTES)
    stride = timedelta(minutes=INTERVAL_DURATION_MINUTES + INTERVAL_GAP_MINUTES)
    for idx in range(CORPUS_INTERVAL_COUNT):
        start = CORPUS_EPOCH + stride * idx
        end = start + duration
        act_id = activity_ids[idx % CORPUS_ACTIVITY_COUNT]
        note = f"Private workstation note #{idx}"
        if idx % CHAOTIC_STRIDE == 0:
            ledger.record_chaotic_interval(
                started_at=start,
                ended_at=end,
                activity_id=act_id,
                notes=note,
            )
        else:
            ledger.record_structured_interval(
                act_id,
                started_at=start,
                ended_at=end,
                notes=note,
            )

    return ledger, outbox_store, standing_mappings


# endregion


# region Benchmark Tests
def test_bench_rollup_preview_1k(tmp_path: Path, benchmark: BenchmarkFixture) -> None:
    """Benchmark `Rollup` + `Outbox` dry-run preview on `ledger-1k` (`p95 <= 0.050s`)."""
    db_path = tmp_path / "ledger_1k_rollup.sqlite3"
    ledger, store, standing_mappings = _seed_ledger_1k_for_rollup(db_path)
    policy = RollupPolicy(
        grouping_window=GROUPING_WINDOW_DAY,
        rounding_quantum_seconds=QUANTUM_FIFTEEN_MINUTES,
        rounding_mode=ROUNDING_MODE_NEAREST,
        redact_notes=True,
        standing_activity_mappings=standing_mappings,
        flush_cadence=FLUSH_CADENCE_EOD,
    )
    engine = RollupEngine(ledger=ledger, outbox_store=store)
    try:

        def _run_preview() -> RollupPreviewResult:
            """Execute full 1,000-interval rollup and Outbox diff dry-run preview."""
            return engine.preview_rollup(sink="jira", policy=policy)

        preview = benchmark(_run_preview)
        assert preview.total_intervals_examined == CORPUS_INTERVAL_COUNT
        assert preview.unmapped_interval_count == 0
        assert len(preview.proposals) > 0
        assert len(preview.diffs) == len(preview.proposals)

        if benchmark.stats is not None:
            p95_seconds = _percentile(benchmark.stats.stats.sorted_data, P95_QUANTILE)
            assert p95_seconds <= ROLLUP_PREVIEW_1K_P95_BUDGET_SECONDS, (
                f"wt_rollup_preview_1k p95={p95_seconds:.6f}s exceeded "
                f"budget={ROLLUP_PREVIEW_1K_P95_BUDGET_SECONDS:.3f}s"
            )
    finally:
        store.close()
        ledger.close()


# endregion
