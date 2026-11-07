"""Behavioural tests for configurable Facets and scoped ContextFacts."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from worktrack.core import (
    BUILTIN_WORKPLACE_FACET_KEY,
    BUILTIN_WORKPLACE_VALUES,
    FACET_TYPE_BOOL,
    FACET_TYPE_ENUM,
    FACET_TYPE_FLOAT,
    FACET_TYPE_INT,
    FACET_TYPE_STRING,
    DuplicateKeyError,
    FacetNotFoundError,
    FacetValueValidationError,
    Ledger,
    LedgerValidationError,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
SAMPLE_START = datetime(2026, 11, 7, 9, 0, 0, tzinfo=UTC)
ONE_HOUR = timedelta(hours=1)
DATE_DAY_ONE = "2026-11-05"
DATE_DAY_TWO = "2026-11-06"
DATE_DAY_THREE = "2026-11-07"
SAMPLE_FOCUS_SCORE = 8
SAMPLE_BILLABLE_MULTIPLIER = 1.25
EXPECTED_ASSERTIONS_TOTAL = 7.0
# endregion


# region Fixtures
@pytest.fixture
def telemetry_recorder(tmp_path: Path) -> TelemetryRecorder:
    """Create an isolated TelemetryRecorder for ContextFact metric assertions."""
    return TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")


@pytest.fixture
def ledger(tmp_path: Path, telemetry_recorder: TelemetryRecorder) -> Iterator[Ledger]:
    """Create a migrated file-backed Ledger with telemetry enabled."""
    instance = Ledger(tmp_path / "facets.sqlite3", telemetry=telemetry_recorder)
    yield instance
    instance.close()


# endregion


# region Facet Tests
def test_builtin_workplace_and_custom_typed_facet_registration(ledger: Ledger) -> None:
    """Verify seeded workplace enum and custom enum/string/int/float/bool Facets."""
    workplace = ledger.get_facet(BUILTIN_WORKPLACE_FACET_KEY)
    assert workplace.value_type == FACET_TYPE_ENUM
    assert workplace.allowed_values == BUILTIN_WORKPLACE_VALUES
    assert workplace.builtin is True

    energy = ledger.register_facet(
        "energy_level", FACET_TYPE_ENUM, allowed_values=("low", "medium", "high")
    )
    focus = ledger.register_facet("focus_score", FACET_TYPE_INT, description="1..10 rating")
    multiplier = ledger.register_facet("rate_multiplier", FACET_TYPE_FLOAT)
    billable = ledger.register_facet("billable", FACET_TYPE_BOOL)
    client_note = ledger.register_facet("location_note", FACET_TYPE_STRING)

    keys = {facet.key for facet in ledger.list_facets()}
    assert {
        BUILTIN_WORKPLACE_FACET_KEY,
        energy.key,
        focus.key,
        multiplier.key,
        billable.key,
        client_note.key,
    } <= keys

    with pytest.raises(DuplicateKeyError):
        ledger.register_facet("focus_score", FACET_TYPE_INT)
    with pytest.raises(LedgerValidationError):
        ledger.register_facet("empty_enum", FACET_TYPE_ENUM, allowed_values=())
    with pytest.raises(LedgerValidationError):
        ledger.register_facet("bad_int", FACET_TYPE_INT, allowed_values=("1",))
    with pytest.raises(FacetNotFoundError):
        ledger.get_facet("nonexistent_facet")


def test_context_fact_rejects_invalid_values_and_ambiguous_scopes(ledger: Ledger) -> None:
    """Verify ContextFact rejects bool-as-int/float, bad enum values, and bad scopes."""
    ledger.register_facet("focus_score", FACET_TYPE_INT)
    ledger.register_facet("rate_multiplier", FACET_TYPE_FLOAT)
    ledger.register_facet("billable", FACET_TYPE_BOOL)
    act = ledger.create_activity("Design Review")
    interval = ledger.record_structured_interval(
        act.id, started_at=SAMPLE_START, ended_at=SAMPLE_START + ONE_HOUR
    )

    with pytest.raises(FacetValueValidationError):
        ledger.assert_context_fact(
            BUILTIN_WORKPLACE_FACET_KEY, "beach_resort", scope_date=DATE_DAY_ONE
        )
    with pytest.raises(FacetValueValidationError):
        ledger.assert_context_fact("focus_score", True, scope_date=DATE_DAY_ONE)
    with pytest.raises(FacetValueValidationError):
        ledger.assert_context_fact("rate_multiplier", False, scope_date=DATE_DAY_ONE)
    with pytest.raises(FacetValueValidationError):
        ledger.assert_context_fact("billable", 1, scope_date=DATE_DAY_ONE)
    with pytest.raises(LedgerValidationError):
        ledger.assert_context_fact(BUILTIN_WORKPLACE_FACET_KEY, "office")
    with pytest.raises(LedgerValidationError):
        ledger.assert_context_fact(
            BUILTIN_WORKPLACE_FACET_KEY, "office", scope_date=DATE_DAY_ONE, interval_id=interval.id
        )


def test_context_fact_upserts_date_and_interval_queries_and_metrics(
    ledger: Ledger, telemetry_recorder: TelemetryRecorder
) -> None:
    """Verify date/interval ContextFact upserts, range queries, and telemetry counter."""
    ledger.register_facet("focus_score", FACET_TYPE_INT)
    ledger.register_facet("rate_multiplier", FACET_TYPE_FLOAT)
    ledger.register_facet("billable", FACET_TYPE_BOOL)
    ledger.register_facet("desk_label", FACET_TYPE_STRING)
    act = ledger.create_activity("Design Review")
    interval = ledger.record_structured_interval(
        act.id, started_at=SAMPLE_START, ended_at=SAMPLE_START + ONE_HOUR
    )

    day_one = ledger.assert_context_fact(
        BUILTIN_WORKPLACE_FACET_KEY, "office", scope_date=DATE_DAY_ONE
    )
    updated_day_one = ledger.assert_context_fact(
        BUILTIN_WORKPLACE_FACET_KEY, "home_office", scope_date=date(2026, 11, 5)
    )
    assert (updated_day_one.id, updated_day_one.value) == (day_one.id, "home_office")

    ledger.assert_context_fact(BUILTIN_WORKPLACE_FACET_KEY, "mobile_work", scope_date=DATE_DAY_TWO)
    ledger.assert_context_fact("focus_score", SAMPLE_FOCUS_SCORE, interval_id=interval.id)
    ledger.assert_context_fact(
        "rate_multiplier", SAMPLE_BILLABLE_MULTIPLIER, interval_id=interval.id
    )
    ledger.assert_context_fact("billable", True, interval_id=interval.id)
    ledger.assert_context_fact("desk_label", "Bldg-4", scope_date=DATE_DAY_THREE)

    date_facts = ledger.list_context_facts(
        facet_key=BUILTIN_WORKPLACE_FACET_KEY, since_date=DATE_DAY_ONE, until_date=DATE_DAY_TWO
    )
    assert [(f.scope_date, f.value) for f in date_facts] == [
        (DATE_DAY_ONE, "home_office"),
        (DATE_DAY_TWO, "mobile_work"),
    ]
    interval_facts = {
        f.facet_key: f.value for f in ledger.list_context_facts(interval_id=interval.id)
    }
    assert interval_facts == {
        "focus_score": SAMPLE_FOCUS_SCORE,
        "rate_multiplier": SAMPLE_BILLABLE_MULTIPLIER,
        "billable": True,
    }
    assert (
        f"worktrack_context_facts_asserted_total {EXPECTED_ASSERTIONS_TOTAL}"
        in telemetry_recorder.render_metrics_text()
    )


# endregion
