"""Behavioural tests for ReportingPack engine, German tax pack, and travel allowances."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest

from worktrack.core import Ledger
from worktrack.reporting import (
    ALLOWED_TRANSPORT_MODES,
    BUILTIN_PACKS,
    DAY_CLASS_ARRIVAL_OR_DEPARTURE,
    DAY_CLASS_FULL_24H,
    DAY_CLASS_SINGLE_OVER_8H,
    DAY_CLASS_SINGLE_UNDER_8H,
    DE_BREAKFAST_DEDUCTION_RATIO,
    DE_CAR_MILEAGE_RATE_PER_KM,
    DE_COMMUTE_FIRST_TIER_KM,
    DE_COMMUTE_FIRST_TIER_RATE,
    DE_COMMUTE_SECOND_TIER_RATE,
    DE_DINNER_DEDUCTION_RATIO,
    DE_HOMEOFFICE_DAILY_RATE,
    DE_HOMEOFFICE_MAX_DAYS,
    DE_LUNCH_DEDUCTION_RATIO,
    DE_PER_DIEM_FULL_RATE,
    DE_PER_DIEM_PARTIAL_RATE,
    GENERIC_PACK,
    GERMANY_PACK,
    PACK_SLUG_DE,
    PACK_SLUG_GENERIC,
    TRANSPORT_MODE_CAR,
    TRANSPORT_MODE_TRAIN,
    DayMealFlags,
    JurisdictionReportSummary,
    ReportingEngine,
    ReportingPack,
    ReportingPackRegistry,
    TravelTrip,
    TravelTripStore,
    calculate_trip_allowance,
    export_report_csv,
    export_report_json,
    export_report_markdown,
    load_custom_holidays_from_toml,
    reporting_summary_csvw_schema,
    reporting_summary_json_schema,
)

# region Constants
SAMPLE_START_DATE = "2026-01-01"
SAMPLE_END_DATE = "2026-12-31"
SAMPLE_ONE_WAY_KM_SHORT = 15.0
SAMPLE_ONE_WAY_KM_LONG = 35.0
EXPECTED_SHORT_COMMUTE_RATE = 4.50
EXPECTED_LONG_COMMUTE_RATE = 11.70
SAMPLE_MAX_HOMEOFFICE_DAYS_COUNT = 220
EXPECTED_GERMAN_MAX_HOMEOFFICE_SUM = 1260.00
EXPECTED_FULL_PER_DIEM = 28.00
EXPECTED_PARTIAL_PER_DIEM = 14.00
SAMPLE_CAR_DISTANCE_KM = 120.0
EXPECTED_CAR_MILEAGE = 36.00
# endregion


# region Pack Tests
def test_builtin_reporting_packs_and_rates() -> None:
    """Verify generic and Germany packs expose exact statutory and baseline rates."""
    assert PACK_SLUG_GENERIC in BUILTIN_PACKS
    assert PACK_SLUG_DE in BUILTIN_PACKS
    assert "car" in ALLOWED_TRANSPORT_MODES

    assert not GENERIC_PACK.tax_rules_enabled
    assert GENERIC_PACK.daily_commute_allowance(SAMPLE_ONE_WAY_KM_LONG) == 0.0

    de = GERMANY_PACK
    assert isinstance(de, ReportingPack)
    assert de.tax_rules_enabled is True
    assert de.home_office_daily_rate == DE_HOMEOFFICE_DAILY_RATE
    assert de.home_office_max_days == DE_HOMEOFFICE_MAX_DAYS
    assert de.commute_first_tier_km == DE_COMMUTE_FIRST_TIER_KM
    assert de.commute_first_tier_rate == DE_COMMUTE_FIRST_TIER_RATE
    assert de.commute_second_tier_rate == DE_COMMUTE_SECOND_TIER_RATE
    assert de.per_diem_partial_day_rate == DE_PER_DIEM_PARTIAL_RATE
    assert de.per_diem_full_day_rate == DE_PER_DIEM_FULL_RATE
    assert de.breakfast_deduction_ratio == DE_BREAKFAST_DEDUCTION_RATIO
    assert de.lunch_deduction_ratio == DE_LUNCH_DEDUCTION_RATIO
    assert de.dinner_deduction_ratio == DE_DINNER_DEDUCTION_RATIO
    assert de.car_mileage_rate_per_km == DE_CAR_MILEAGE_RATE_PER_KM

    assert de.daily_commute_allowance(SAMPLE_ONE_WAY_KM_SHORT) == EXPECTED_SHORT_COMMUTE_RATE
    assert de.daily_commute_allowance(SAMPLE_ONE_WAY_KM_LONG) == EXPECTED_LONG_COMMUTE_RATE
    assert de.daily_commute_allowance(0.0) == 0.0


def test_reporting_pack_registry_and_toml_override(tmp_path: Path) -> None:
    """Verify registry parses custom packs and holidays from TOML config."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
        [reporting.custom_holidays]
        "2026-12-24" = "Heiligabend"
        "2026-12-31" = "Silvester"

        [reporting.packs.at]
        title = "Austria"
        currency = "EUR"
        country_code = "AT"
        tax_rules_enabled = true
        home_office_daily_rate = 3.00
        home_office_max_days = 100
        commute_first_tier_km = 20.0
        commute_first_tier_rate = 0.20
        commute_second_tier_rate = 0.20
        per_diem_partial_day_rate = 13.20
        per_diem_full_day_rate = 26.40
        """,
        encoding="utf-8",
    )
    registry = ReportingPackRegistry.from_toml_file(cfg_file)
    assert len(registry.list_packs()) >= 3
    at_pack = registry.get_pack("at")
    assert at_pack.title == "Austria"
    assert at_pack.home_office_daily_rate == 3.00

    holidays = load_custom_holidays_from_toml(cfg_file)
    assert holidays["2026-12-24"] == "Heiligabend"
    assert holidays["2026-12-31"] == "Silvester"

    with pytest.raises(KeyError):
        registry.get_pack("nonexistent_pack")
# endregion


# region Travel Tests
def test_travel_store_lifecycle_and_filtering(tmp_path: Path) -> None:
    """Verify TravelTripStore CRUD operations and date-range filtering."""
    store = TravelTripStore(tmp_path / "trips.sqlite3")
    with store:
        assert store.list_trips() == []

        trip = store.record_trip(
            title="Client Workshop Berlin",
            destination="Berlin",
            started_at="2026-03-10T07:30:00Z",
            ended_at="2026-03-12T19:00:00Z",
            country_code="DE",
            transport_mode=TRANSPORT_MODE_TRAIN,
            distance_km=0.0,
            meals_by_date={
                "2026-03-11": DayMealFlags(breakfast=True, lunch=True, dinner=False)
            },
            notes="Strategy kick-off",
        )
        assert trip.id
        assert trip.touched_dates() == ["2026-03-10", "2026-03-11", "2026-03-12"]

        retrieved = store.get_trip(trip.id)
        assert retrieved.destination == "Berlin"
        assert retrieved.meals_by_date["2026-03-11"].lunch is True

        march_trips = store.list_trips(start_date="2026-03-01", end_date="2026-03-31")
        assert len(march_trips) == 1

        april_trips = store.list_trips(start_date="2026-04-01", end_date="2026-04-30")
        assert len(april_trips) == 0

        travel_dates = store.travel_dates_in_range("2026-03-01", "2026-03-31")
        assert travel_dates == {"2026-03-11"}

        assert store.delete_trip(trip.id) is True
        assert store.list_trips() == []


def test_calculate_trip_allowance_single_day() -> None:
    """Verify single-day trip allowances under and over 8 hours with meal deductions."""
    short_single = TravelTrip(
        id="t1",
        title="Quick local meeting",
        destination="Munich",
        country_code="DE",
        started_at="2026-06-01T09:00:00Z",
        ended_at="2026-06-01T15:00:00Z",
        transport_mode=TRANSPORT_MODE_TRAIN,
        distance_km=0.0,
        meals_by_date={},
        entity_id=None,
        notes="",
        created_at="2026-06-01T08:00:00Z",
    )
    assert short_single.duration_hours() == 6.0
    calc_short = calculate_trip_allowance(short_single, GERMANY_PACK)
    assert calc_short.days[0].classification == DAY_CLASS_SINGLE_UNDER_8H
    assert calc_short.total_reimbursement == 0.0

    long_single = TravelTrip(
        id="t2",
        title="Full day conference",
        destination="Frankfurt",
        country_code="DE",
        started_at="2026-06-02T08:00:00Z",
        ended_at="2026-06-02T18:30:00Z",
        transport_mode=TRANSPORT_MODE_CAR,
        distance_km=SAMPLE_CAR_DISTANCE_KM,
        meals_by_date={"2026-06-02": DayMealFlags(lunch=True)},
        entity_id=None,
        notes="",
        created_at="2026-06-02T07:00:00Z",
    )
    calc_long = calculate_trip_allowance(long_single, GERMANY_PACK)
    assert calc_long.days[0].classification == DAY_CLASS_SINGLE_OVER_8H
    assert calc_long.days[0].gross_per_diem == EXPECTED_PARTIAL_PER_DIEM
    assert calc_long.days[0].meal_deduction == round(0.40 * EXPECTED_FULL_PER_DIEM, 2)
    assert calc_long.days[0].net_per_diem == round(14.00 - 11.20, 2)
    assert calc_long.mileage_allowance == EXPECTED_CAR_MILEAGE
    assert calc_long.total_reimbursement == round(2.80 + EXPECTED_CAR_MILEAGE, 2)


def test_calculate_trip_allowance_multiday() -> None:
    """Verify multi-day trip arrival, intermediate 24h, and departure allowances."""
    multi_day = TravelTrip(
        id="t3",
        title="3-day workshop",
        destination="Hamburg",
        country_code="DE",
        started_at="2026-06-10T10:00:00Z",
        ended_at="2026-06-12T17:00:00Z",
        transport_mode=TRANSPORT_MODE_TRAIN,
        distance_km=0.0,
        meals_by_date={
            "2026-06-10": DayMealFlags(dinner=True),
            "2026-06-11": DayMealFlags(breakfast=True, lunch=True, dinner=True),
            "2026-06-12": DayMealFlags(breakfast=True),
        },
        entity_id=None,
        notes="",
        created_at="2026-06-10T09:00:00Z",
    )
    calc_multi = calculate_trip_allowance(multi_day, GERMANY_PACK)
    assert len(calc_multi.days) == 3
    assert calc_multi.days[0].classification == DAY_CLASS_ARRIVAL_OR_DEPARTURE
    assert calc_multi.days[0].net_per_diem == round(14.00 - (0.40 * 28.00), 2)
    assert calc_multi.days[1].classification == DAY_CLASS_FULL_24H
    assert calc_multi.days[1].net_per_diem == 0.00
    assert calc_multi.days[2].classification == DAY_CLASS_ARRIVAL_OR_DEPARTURE
    assert calc_multi.days[2].net_per_diem == round(14.00 - (0.20 * 28.00), 2)
# endregion


# region Engine and Exporter Tests
def _seed_reporting_data(ledger: Ledger, trip_store: TravelTripStore) -> None:
    """Seed test workplace context facts and sample business trips."""
    base_d = date(2026, 1, 1)
    for offset in range(220):
        date_str = (base_d + timedelta(days=offset)).isoformat()
        ledger.assert_context_fact("workplace", "home_office", scope_date=date_str)

    ledger.assert_context_fact("workplace", "office", scope_date="2026-10-01")
    ledger.assert_context_fact("workplace", "office", scope_date="2026-10-02")
    ledger.assert_context_fact("workplace", "mobile_work", scope_date="2026-10-05")

    trip_store.record_trip(
        title="Project Meet",
        destination="Stuttgart",
        started_at="2026-05-18T08:00:00Z",
        ended_at="2026-05-20T18:00:00Z",
        country_code="DE",
        transport_mode=TRANSPORT_MODE_CAR,
        distance_km=200.0,
    )


def test_reporting_engine_build_summary_germany(tmp_path: Path) -> None:
    """Verify build_summary calculations and exclusions under German tax rules."""
    ledger_db = tmp_path / "ledger.sqlite3"
    with Ledger(ledger_db) as ledger:
        trip_store = TravelTripStore(tmp_path / "trips.sqlite3")
        _seed_reporting_data(ledger, trip_store)

        engine = ReportingEngine()
        summary_de = engine.build_summary(
            ledger=ledger,
            pack=GERMANY_PACK,
            start_date=SAMPLE_START_DATE,
            end_date=SAMPLE_END_DATE,
            office_one_way_km=SAMPLE_ONE_WAY_KM_LONG,
            excluded_dates={"2026-01-01"},
            travel_store=trip_store,
        )
        assert summary_de.home_office_days_raw >= 210
        assert summary_de.home_office_days_capped <= DE_HOMEOFFICE_MAX_DAYS
        assert summary_de.home_office_allowance == round(
            summary_de.home_office_days_capped * DE_HOMEOFFICE_DAILY_RATE, 2
        )
        assert summary_de.office_days_eligible == 2
        assert summary_de.commute_daily_allowance == EXPECTED_LONG_COMMUTE_RATE
        assert summary_de.commute_allowance_total == round(2 * EXPECTED_LONG_COMMUTE_RATE, 2)
        assert summary_de.mobile_work_days == 1
        assert "2026-05-19" in summary_de.excluded_travel_dates
        assert summary_de.total_allowance_and_reimbursement > 0.0


def test_reporting_engine_build_summary_generic(tmp_path: Path) -> None:
    """Verify build_summary produces zero tax allowances for generic pack."""
    ledger_db = tmp_path / "ledger.sqlite3"
    with Ledger(ledger_db) as ledger:
        trip_store = TravelTripStore(tmp_path / "trips.sqlite3")
        _seed_reporting_data(ledger, trip_store)

        engine = ReportingEngine()
        summary_generic = engine.build_summary(
            ledger=ledger,
            pack=GENERIC_PACK,
            start_date=SAMPLE_START_DATE,
            end_date=SAMPLE_END_DATE,
            office_one_way_km=SAMPLE_ONE_WAY_KM_LONG,
            travel_store=trip_store,
        )
        assert summary_generic.home_office_allowance == 0.0
        assert summary_generic.commute_allowance_total == 0.0
        assert summary_generic.travel_allowance_total == 0.0
        assert summary_generic.total_allowance_and_reimbursement == 0.0


def test_reporting_exporters_and_schema_validation(tmp_path: Path) -> None:
    """Verify JSON, CSV, and Markdown exports conform to schemas."""
    summary = JurisdictionReportSummary(
        pack=GERMANY_PACK,
        start_date="2026-01-01",
        end_date="2026-12-31",
        office_one_way_km=30.0,
        home_office_days_raw=100,
        home_office_days_eligible=98,
        home_office_days_capped=98,
        home_office_allowance=588.00,
        office_days_raw=20,
        office_days_eligible=20,
        commute_daily_allowance=9.80,
        commute_allowance_total=196.00,
        mobile_work_days=2,
        client_site_days=1,
        excluded_absence_or_holiday_dates=("2026-01-01", "2026-12-25"),
        excluded_travel_dates=("2026-06-15",),
        trips=(),
        travel_per_diem_total=0.0,
        travel_mileage_total=0.0,
        travel_allowance_total=0.0,
        absence_summary=None,
        total_allowance_and_reimbursement=784.00,
    )

    raw_json = export_report_json(summary)
    parsed = json.loads(raw_json)
    json_schema = reporting_summary_json_schema()
    jsonschema.validate(instance=parsed, schema=json_schema)

    raw_csv = export_report_csv(summary)
    reader = csv.reader(io.StringIO(raw_csv))
    rows = list(reader)
    assert len(rows) > 5
    assert rows[0] == ["section", "item", "metric", "value", "unit"]

    csvw = reporting_summary_csvw_schema()
    assert csvw["@context"] == "http://www.w3.org/ns/csvw"
    assert "tableSchema" in csvw

    md = export_report_markdown(summary)
    assert "# Jurisdiction Report" in md
    assert "Home Office Allowance" in md
    assert "784.00" in md
# endregion
