"""Behavioural tests for non-strict Entities and cycle-checked Attribution DAG."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worktrack.core import (
    ENTITY_KIND_COMPONENT,
    ENTITY_KIND_COST_CENTER,
    ENTITY_KIND_CUSTOMER,
    ENTITY_KIND_EPIC,
    ENTITY_KIND_PROJECT,
    AttributionCycleError,
    InvalidAttributionWeightError,
    Ledger,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
DAY_START = datetime(2026, 11, 7, 8, 0, 0, tzinfo=UTC)
ONE_HOUR = timedelta(hours=1)
TWO_HOURS = timedelta(hours=2)
WEIGHT_FULL = 1.0
WEIGHT_SIXTY_PCT = 0.6
WEIGHT_FORTY_PCT = 0.4
WEIGHT_FIFTY_PCT = 0.5
WEIGHT_ZERO = 0.0
WEIGHT_OVER_ONE = 1.2
EXPECTED_CUST_A_SECONDS = 4320.0
EXPECTED_CUST_B_SECONDS = 2880.0
EXPECTED_TOTAL_SECONDS = 7200.0
# endregion


# region Fixtures
@pytest.fixture
def telemetry_recorder(tmp_path: Path) -> TelemetryRecorder:
    """Create an isolated TelemetryRecorder for attribution query assertions."""
    return TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")


@pytest.fixture
def ledger(tmp_path: Path, telemetry_recorder: TelemetryRecorder) -> Iterator[Ledger]:
    """Create a migrated file-backed Ledger with telemetry enabled."""
    instance = Ledger(tmp_path / "attribution.sqlite3", telemetry=telemetry_recorder)
    yield instance
    instance.close()


# endregion


# region Attribution Tests
def test_non_strict_entities_and_clock_in_without_entities(ledger: Ledger) -> None:
    """Verify Activities clock in without Entities and support all Entity kinds."""
    solo_activity = ledger.create_activity("Ad-hoc Exploration")
    active = ledger.clock_in(solo_activity.id, started_at=DAY_START)
    closed = ledger.clock_out(ended_at=DAY_START + ONE_HOUR, interval_id=active.id)
    assert closed.duration_seconds == ONE_HOUR.total_seconds()

    project = ledger.create_entity(
        ENTITY_KIND_PROJECT, "wt-core", "Worktrack Core", metadata={"tier": "p0"}
    )
    component = ledger.create_entity(ENTITY_KIND_COMPONENT, "ledger", "SQLite Ledger")
    customer = ledger.create_entity(ENTITY_KIND_CUSTOMER, "acme", "Acme GmbH")
    epic = ledger.create_entity(ENTITY_KIND_EPIC, "q4-mvp", "Q4 MVP")
    cost_center = ledger.create_entity(ENTITY_KIND_COST_CENTER, "cc-4200", "R&D Engineering")
    custom_team = ledger.create_entity("program", "privacy-first", "Privacy Initiative")

    assert project.slug == "wt-core"
    assert project.metadata == {"tier": "p0"}
    assert ledger.find_entity(ENTITY_KIND_CUSTOMER, "acme") == customer
    assert len(ledger.list_entities()) == len(
        (project, component, customer, epic, cost_center, custom_team)
    )
    assert ledger.list_entities(kind=ENTITY_KIND_COMPONENT) == [component]


def test_attribution_rejects_cycles_and_invalid_weights(ledger: Ledger) -> None:
    """Verify Entity -> Entity cycle rejection and weight range/sum validation."""
    act = ledger.create_activity("Platform Work")
    comp_a = ledger.create_entity(ENTITY_KIND_COMPONENT, "auth", "Auth Broker")
    comp_b = ledger.create_entity(ENTITY_KIND_PROJECT, "platform", "Platform")
    cust_a = ledger.create_entity(ENTITY_KIND_CUSTOMER, "alpha", "Alpha Corp")
    cust_b = ledger.create_entity(ENTITY_KIND_CUSTOMER, "beta", "Beta Corp")

    for invalid_weight in (WEIGHT_ZERO, -WEIGHT_FORTY_PCT, WEIGHT_OVER_ONE, float("nan")):
        with pytest.raises(InvalidAttributionWeightError):
            ledger.set_attribution(
                source_activity_id=act.id,
                target_entity_id=comp_a.id,
                weight=invalid_weight,
            )

    with pytest.raises(AttributionCycleError):
        ledger.set_attribution(
            source_entity_id=comp_a.id, target_entity_id=comp_a.id, weight=WEIGHT_FULL
        )

    ledger.set_attribution(
        source_entity_id=comp_a.id, target_entity_id=comp_b.id, weight=WEIGHT_FULL
    )
    ledger.set_attribution(
        source_entity_id=comp_b.id, target_entity_id=cust_a.id, weight=WEIGHT_SIXTY_PCT
    )

    with pytest.raises(AttributionCycleError):
        ledger.set_attribution(
            source_entity_id=cust_a.id, target_entity_id=comp_a.id, weight=WEIGHT_FULL
        )

    with pytest.raises(InvalidAttributionWeightError):
        ledger.set_attribution(
            source_entity_id=comp_b.id,
            target_entity_id=cust_b.id,
            weight=WEIGHT_SIXTY_PCT,
            enforce_weight_sum=True,
        )

    ledger.set_attribution(
        source_entity_id=comp_b.id,
        target_entity_id=cust_b.id,
        weight=WEIGHT_FORTY_PCT,
        enforce_weight_sum=True,
    )
    sums = ledger.validate_outgoing_weights(source_entity_id=comp_b.id)
    assert sums[ENTITY_KIND_CUSTOMER] == pytest.approx(WEIGHT_FULL)


def test_query_entity_attribution_multihop_clipping_and_telemetry(
    ledger: Ledger, telemetry_recorder: TelemetryRecorder
) -> None:
    """Verify multi-hop DAG duration rollup, clipping, normalization, and metrics."""
    ticket = ledger.create_activity("PROJ-500 Billing", external_ref="PROJ-500")
    billing = ledger.create_entity(ENTITY_KIND_COMPONENT, "billing", "Billing Engine")
    cust_a = ledger.create_entity(ENTITY_KIND_CUSTOMER, "cust-a", "Customer A")
    cust_b = ledger.create_entity(ENTITY_KIND_CUSTOMER, "cust-b", "Customer B")

    ledger.set_attribution(
        source_activity_id=ticket.id, target_entity_id=billing.id, weight=WEIGHT_FULL
    )
    ledger.set_attribution(
        source_entity_id=billing.id, target_entity_id=cust_a.id, weight=WEIGHT_SIXTY_PCT
    )
    ledger.set_attribution(
        source_entity_id=billing.id, target_entity_id=cust_b.id, weight=WEIGHT_FORTY_PCT
    )
    ledger.record_structured_interval(
        ticket.id, started_at=DAY_START, ended_at=DAY_START + TWO_HOURS
    )

    by_key = {
        row.entity.key: row
        for row in ledger.query_entity_attribution(
            since=DAY_START, until=DAY_START + TWO_HOURS, target_kind=ENTITY_KIND_CUSTOMER
        )
    }
    assert by_key["cust-a"].attributed_seconds == pytest.approx(EXPECTED_CUST_A_SECONDS)
    assert by_key["cust-b"].attributed_seconds == pytest.approx(EXPECTED_CUST_B_SECONDS)
    assert by_key["cust-a"].share_of_kind == pytest.approx(WEIGHT_SIXTY_PCT)

    clipped = ledger.query_entity_attribution(
        since=DAY_START + ONE_HOUR,
        until=DAY_START + TWO_HOURS,
        target_kind=ENTITY_KIND_COMPONENT,
    )
    assert clipped[0].attributed_seconds == pytest.approx(ONE_HOUR.total_seconds())

    ledger.set_attribution(
        source_entity_id=billing.id, target_entity_id=cust_b.id, weight=WEIGHT_SIXTY_PCT
    )
    normalized = ledger.query_entity_attribution(target_kind=ENTITY_KIND_CUSTOMER, normalize=True)
    assert all(
        s.attributed_seconds == pytest.approx(EXPECTED_TOTAL_SECONDS * WEIGHT_FIFTY_PCT)
        for s in normalized
    )
    assert "worktrack_attribution_queries_total 3.0" in telemetry_recorder.render_metrics_text()


def test_query_entity_attribution_diamond_dag_and_edge_removal(ledger: Ledger) -> None:
    """Verify diamond DAG path convergence and Attribution edge removal."""
    ticket = ledger.create_activity("Diamond Feature")
    comp_one = ledger.create_entity(ENTITY_KIND_COMPONENT, "api", "API Gateway")
    comp_two = ledger.create_entity(ENTITY_KIND_COMPONENT, "worker", "Async Worker")
    customer = ledger.create_entity(ENTITY_KIND_CUSTOMER, "converged", "Converged Customer")

    ledger.set_attribution(
        source_activity_id=ticket.id, target_entity_id=comp_one.id, weight=WEIGHT_SIXTY_PCT
    )
    edge_two = ledger.set_attribution(
        source_activity_id=ticket.id, target_entity_id=comp_two.id, weight=WEIGHT_FORTY_PCT
    )
    ledger.set_attribution(
        source_entity_id=comp_one.id, target_entity_id=customer.id, weight=WEIGHT_FULL
    )
    ledger.set_attribution(
        source_entity_id=comp_two.id, target_entity_id=customer.id, weight=WEIGHT_FULL
    )
    ledger.record_structured_interval(
        ticket.id, started_at=DAY_START, ended_at=DAY_START + TWO_HOURS
    )

    converged = ledger.query_entity_attribution(target_kind=ENTITY_KIND_CUSTOMER)
    assert len(converged) == 1
    assert converged[0].attributed_seconds == pytest.approx(EXPECTED_TOTAL_SECONDS)

    ledger.remove_attribution(edge_two.id)
    after_remove = ledger.query_entity_attribution(target_kind=ENTITY_KIND_CUSTOMER)
    assert after_remove[0].attributed_seconds == pytest.approx(EXPECTED_CUST_A_SECONDS)


# endregion
