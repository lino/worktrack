"""Benchmark test (`BENCH-SPEC-WT`) for `ledger-1k` Entity Attribution rollup."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pytest_benchmark.fixture import BenchmarkFixture

from worktrack.core import (
    ACTIVITY_KIND_STANDING,
    ACTIVITY_KIND_TARGETED,
    BUILTIN_WORKPLACE_FACET_KEY,
    BUILTIN_WORKPLACE_VALUES,
    ENTITY_KIND_COMPONENT,
    ENTITY_KIND_CUSTOMER,
    EntityAttributionShare,
    Ledger,
)

# region Constants
CORPUS_INTERVAL_COUNT = 1_000
CORPUS_ACTIVITY_COUNT = 120
CORPUS_ATTRIBUTED_ACTIVITIES = 30
CORPUS_COMPONENT_COUNT = 5
CORPUS_CUSTOMER_COUNT = 2
CORPUS_DAILY_FACT_DAYS = 90
INTERVAL_DURATION_MINUTES = 30
INTERVAL_GAP_MINUTES = 5
WEIGHT_FULL = 1.0
WEIGHT_CUST_PRIMARY = 0.6
WEIGHT_CUST_SECONDARY = 0.4
P95_QUANTILE = 0.95
ENTITY_ATTRIBUTION_P95_BUDGET_SECONDS = 0.035
CORPUS_EPOCH = datetime(2026, 8, 1, 8, 0, 0, tzinfo=UTC)
CORPUS_START_DATE = date(2026, 8, 1)
# endregion


# region Fixtures
def _percentile(sorted_samples: Sequence[float], quantile: float) -> float:
    """Compute nearest-rank percentile from a non-empty sorted sample list."""
    idx = min(len(sorted_samples) - 1, max(0, math.ceil(quantile * len(sorted_samples)) - 1))
    return float(sorted_samples[idx])


def _seed_entities_and_attributions(ledger: Ledger, activity_ids: Sequence[str]) -> tuple[str, str]:
    """Create 5 Components + 2 Customers and exactly 40 weighted Attribution edges."""
    cust_primary = ledger.create_entity(ENTITY_KIND_CUSTOMER, "cust-primary", "Primary Corp")
    cust_secondary = ledger.create_entity(ENTITY_KIND_CUSTOMER, "cust-secondary", "Secondary Corp")
    component_ids: list[str] = []
    for comp_idx in range(CORPUS_COMPONENT_COUNT):
        comp = ledger.create_entity(
            ENTITY_KIND_COMPONENT, f"comp-{comp_idx}", f"Component {comp_idx}"
        )
        component_ids.append(comp.id)
        ledger.set_attribution(
            source_entity_id=comp.id,
            target_entity_id=cust_primary.id,
            weight=WEIGHT_CUST_PRIMARY,
        )
        ledger.set_attribution(
            source_entity_id=comp.id,
            target_entity_id=cust_secondary.id,
            weight=WEIGHT_CUST_SECONDARY,
        )
    for act_idx in range(CORPUS_ATTRIBUTED_ACTIVITIES):
        target_comp = component_ids[act_idx % CORPUS_COMPONENT_COUNT]
        ledger.set_attribution(
            source_activity_id=activity_ids[act_idx],
            target_entity_id=target_comp,
            weight=WEIGHT_FULL,
        )
    return cust_primary.key, cust_secondary.key


def _seed_ledger_1k(db_path: Path) -> tuple[Ledger, float]:
    """Build canonical `ledger-1k` corpus (1k intervals, 120 activities, 40 edges, 90 facts)."""
    ledger = Ledger(db_path)
    activity_ids: list[str] = []
    for idx in range(CORPUS_ACTIVITY_COUNT):
        kind = ACTIVITY_KIND_STANDING if idx % 6 == 0 else ACTIVITY_KIND_TARGETED
        act = ledger.create_activity(f"Activity {idx}", kind=kind, external_ref=f"WT-{idx + 1}")
        activity_ids.append(act.id)

    _seed_entities_and_attributions(ledger, activity_ids)

    attributed_seconds = 0.0
    duration = timedelta(minutes=INTERVAL_DURATION_MINUTES)
    stride = timedelta(minutes=INTERVAL_DURATION_MINUTES + INTERVAL_GAP_MINUTES)
    for idx in range(CORPUS_INTERVAL_COUNT):
        start = CORPUS_EPOCH + stride * idx
        end = start + duration
        act_idx = idx % CORPUS_ACTIVITY_COUNT
        act_id = activity_ids[act_idx]
        if idx % 4 == 0:
            ledger.record_chaotic_interval(started_at=start, ended_at=end, activity_id=act_id)
        else:
            ledger.record_structured_interval(act_id, started_at=start, ended_at=end)
        if act_idx < CORPUS_ATTRIBUTED_ACTIVITIES:
            attributed_seconds += duration.total_seconds()

    for day_offset in range(CORPUS_DAILY_FACT_DAYS):
        fact_date = CORPUS_START_DATE + timedelta(days=day_offset)
        workplace_val = BUILTIN_WORKPLACE_VALUES[day_offset % len(BUILTIN_WORKPLACE_VALUES)]
        ledger.assert_context_fact(BUILTIN_WORKPLACE_FACET_KEY, workplace_val, scope_date=fact_date)

    return ledger, attributed_seconds


# endregion


# region Benchmark Tests
def test_bench_entity_attribution_query(tmp_path: Path, benchmark: BenchmarkFixture) -> None:
    """Benchmark `query_entity_attribution` against `ledger-1k` (`p95 <= 0.035s`)."""
    ledger, expected_attributed_seconds = _seed_ledger_1k(tmp_path / "ledger_1k.sqlite3")
    try:
        assert len(ledger.list_attributions()) == (
            CORPUS_COMPONENT_COUNT * CORPUS_CUSTOMER_COUNT + CORPUS_ATTRIBUTED_ACTIVITIES
        )

        def _run_query() -> list[EntityAttributionShare]:
            """Execute customer-level attribution rollup across the full corpus."""
            return ledger.query_entity_attribution(target_kind=ENTITY_KIND_CUSTOMER)

        shares = benchmark(_run_query)
        by_key = {row.entity.key: row.attributed_seconds for row in shares}
        assert by_key["cust-primary"] == pytest.approx(
            expected_attributed_seconds * WEIGHT_CUST_PRIMARY
        )
        assert by_key["cust-secondary"] == pytest.approx(
            expected_attributed_seconds * WEIGHT_CUST_SECONDARY
        )

        if benchmark.stats is not None:
            p95_seconds = _percentile(benchmark.stats.stats.sorted_data, P95_QUANTILE)
            assert p95_seconds <= ENTITY_ATTRIBUTION_P95_BUDGET_SECONDS, (
                f"wt_entity_attribution_query p95={p95_seconds:.6f}s exceeded "
                f"budget={ENTITY_ATTRIBUTION_P95_BUDGET_SECONDS:.3f}s"
            )
    finally:
        ledger.close()


# endregion
