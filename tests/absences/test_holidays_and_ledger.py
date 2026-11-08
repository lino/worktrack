"""Behavioural unit tests for German holiday calendars and absence ledger."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from worktrack.absences import (
    ABSENCE_KIND_COMPENSATORY_OFF,
    ABSENCE_KIND_SICK_LEAVE,
    ABSENCE_KIND_SPECIAL_LEAVE,
    ABSENCE_KIND_UNPAID_LEAVE,
    ABSENCE_KIND_VACATION,
    DEFAULT_ANNUAL_VACATION_DAYS,
    GERMAN_SUBDIVISIONS,
    AbsenceEntry,
    AbsenceLedger,
    HolidayCalendar,
    PublicHoliday,
    VacationQuota,
    VacationSummary,
)

# region Constants
YEAR_2024 = 2024
YEAR_2025 = 2025
YEAR_2026 = 2026
EASTER_MONTH_MARCH = 3
EASTER_DAY_2024 = 31
EASTER_MONTH_APRIL = 4
EASTER_DAY_2025 = 20
EASTER_DAY_2026 = 5
BUSS_UND_BETTAG_DAY_2024 = 20
BUSS_UND_BETTAG_DAY_2025 = 19
BUSS_UND_BETTAG_DAY_2026 = 18

EXPECTED_NATIONWIDE_COUNT = 9
EXPECTED_BW_COUNT = 12
EXPECTED_BY_COUNT = 13
EXPECTED_BE_COUNT = 10
EXPECTED_BB_COUNT = 10
EXPECTED_SN_COUNT = 11

HALF_DAY_FRACTION = 0.5
FULL_DAY_FRACTION = 1.0
INVALID_DAY_FRACTION = 0.75

CUSTOM_ENTITLEMENT_DAYS = 28.0
CUSTOM_CARRYOVER_DAYS = 4.0
TOTAL_CUSTOM_DAYS = 32.0

SAMPLE_TOML_CONTENT = """
subdivision = "DE-BY"

[holidays]
"2025-12-24" = "Heiligabend"
"2025-12-31" = "Silvester"
"""
# endregion


# region Holiday Calendar Tests
def test_easter_sunday_and_subdivisions_calculation() -> None:
    """Verify Easter Sunday dates and nationwide holiday generation."""
    cal_de = HolidayCalendar(subdivision="DE")
    hols_2024 = {h.name: h.day for h in cal_de.holidays_for_year(YEAR_2024)}
    hols_2025 = {h.name: h.day for h in cal_de.holidays_for_year(YEAR_2025)}
    hols_2026 = {h.name: h.day for h in cal_de.holidays_for_year(YEAR_2026)}

    assert hols_2024["Karfreitag"] == date(YEAR_2024, EASTER_MONTH_MARCH, 29)
    assert hols_2024["Ostermontag"] == date(YEAR_2024, EASTER_MONTH_APRIL, 1)
    assert hols_2025["Ostermontag"] == date(YEAR_2025, EASTER_MONTH_APRIL, 21)
    assert hols_2026["Ostermontag"] == date(YEAR_2026, EASTER_MONTH_APRIL, 6)
    assert len(cal_de.holidays_for_year(YEAR_2025)) == EXPECTED_NATIONWIDE_COUNT


def test_all_sixteen_german_subdivisions() -> None:
    """Test all 16 German Bundesländer presets and specific regional holidays."""
    assert len(GERMAN_SUBDIVISIONS) == 16
    for sub in GERMAN_SUBDIVISIONS:
        cal = HolidayCalendar(subdivision=sub)
        hols = cal.holidays_for_year(YEAR_2025)
        names = {h.name for h in hols}
        assert "Neujahr" in names
        assert "Tag der Deutschen Einheit" in names

    cal_bw = HolidayCalendar(subdivision="DE-BW")
    assert len(cal_bw.holidays_for_year(YEAR_2025)) == EXPECTED_BW_COUNT
    assert "Fronleichnam" in {h.name for h in cal_bw.holidays_for_year(YEAR_2025)}

    cal_by = HolidayCalendar(subdivision="DE-BY")
    assert len(cal_by.holidays_for_year(YEAR_2025)) == EXPECTED_BY_COUNT
    assert "Mariä Himmelfahrt" in {h.name for h in cal_by.holidays_for_year(YEAR_2025)}

    cal_be = HolidayCalendar(subdivision="DE-BE")
    assert len(cal_be.holidays_for_year(YEAR_2025)) == EXPECTED_BE_COUNT
    assert "Internationaler Frauentag" in {h.name for h in cal_be.holidays_for_year(YEAR_2025)}

    cal_th = HolidayCalendar(subdivision="DE-TH")
    assert "Weltkindertag" in {h.name for h in cal_th.holidays_for_year(YEAR_2025)}

    cal_sn = HolidayCalendar(subdivision="DE-SN")
    assert len(cal_sn.holidays_for_year(YEAR_2025)) == EXPECTED_SN_COUNT
    sn_hols_2024 = {h.name: h.day for h in cal_sn.holidays_for_year(YEAR_2024)}
    sn_hols_2025 = {h.name: h.day for h in cal_sn.holidays_for_year(YEAR_2025)}
    sn_hols_2026 = {h.name: h.day for h in cal_sn.holidays_for_year(YEAR_2026)}
    assert sn_hols_2024["Buß- und Bettag"] == date(YEAR_2024, 11, BUSS_UND_BETTAG_DAY_2024)
    assert sn_hols_2025["Buß- und Bettag"] == date(YEAR_2025, 11, BUSS_UND_BETTAG_DAY_2025)
    assert sn_hols_2026["Buß- und Bettag"] == date(YEAR_2026, 11, BUSS_UND_BETTAG_DAY_2026)


def test_calendar_validation_and_workday_checks() -> None:
    """Validate subdivision validation and workday determination."""
    with pytest.raises(ValueError, match="Unknown subdivision code"):
        HolidayCalendar(subdivision="DE-INVALID")

    cal_none = HolidayCalendar(subdivision="")
    assert cal_none.holidays_for_year(YEAR_2025) == []

    cal_de = HolidayCalendar(subdivision="DE")
    assert not cal_de.is_workday("2025-01-01")  # Neujahr (Wednesday)
    assert cal_de.is_workday("2025-01-02")  # Thursday
    assert not cal_de.is_workday("2025-01-04")  # Saturday
    assert cal_de.is_public_holiday(date(2025, 1, 1))
    assert cal_de.holiday_name("2025-01-01") == "Neujahr"
    assert cal_de.holiday_name("2025-01-02") is None


def test_custom_holidays_and_toml_loading(tmp_path: Path) -> None:
    """Verify custom holidays mapping and TOML file deserialization."""
    mapping = {"2025-12-24": "Heiligabend", "2025-12-31": "Silvester"}
    cal_custom = HolidayCalendar(subdivision="DE-BY", custom_holidays=mapping)
    assert cal_custom.is_public_holiday("2025-12-24")
    assert cal_custom.holiday_name("2025-12-24") == "Heiligabend"

    seq_holiday = PublicHoliday(
        day=date(2025, 6, 20),
        name="Company Foundation Day",
        subdivision="CUSTOM",
        is_custom=True,
    )
    cal_seq = HolidayCalendar(subdivision="DE", custom_holidays=[seq_holiday])
    assert cal_seq.is_public_holiday("2025-06-20")

    toml_file = tmp_path / "custom_holidays.toml"
    toml_file.write_text(SAMPLE_TOML_CONTENT, encoding="utf-8")
    cal_toml = HolidayCalendar.from_toml(toml_file)
    assert cal_toml.subdivision == "DE-BY"
    assert cal_toml.is_public_holiday("2025-12-24")
    assert cal_toml.is_public_holiday("2025-01-06")  # Heilige Drei Könige in DE-BY
# endregion


# region Absence Entry Tests
def test_absence_entry_workdays_and_effective_days() -> None:
    """Verify AbsenceEntry workday deduction over weekends and statutory holidays."""
    cal = HolidayCalendar(subdivision="DE")
    # 2025-04-18 is Karfreitag, 2025-04-19 Saturday, 2025-04-20 Sunday, 2025-04-21 Ostermontag
    entry = AbsenceEntry(
        id="abs_1",
        start_date="2025-04-17",  # Thursday (workday)
        end_date="2025-04-22",  # Tuesday (workday)
        kind=ABSENCE_KIND_VACATION,
        day_fraction=FULL_DAY_FRACTION,
        notes="Easter getaway",
        created_at="2025-01-01T00:00:00Z",
    )
    workdays = entry.workdays_covered(cal)
    assert workdays == ["2025-04-17", "2025-04-22"]
    assert entry.effective_days(cal) == 2.0

    entry_half = AbsenceEntry(
        id="abs_half",
        start_date="2025-04-17",
        end_date="2025-04-17",
        kind=ABSENCE_KIND_VACATION,
        day_fraction=HALF_DAY_FRACTION,
        notes="Half day morning off",
        created_at="2025-01-01T00:00:00Z",
    )
    assert entry_half.effective_days(cal) == HALF_DAY_FRACTION
    assert entry_half.to_dict(cal)["effective_days"] == HALF_DAY_FRACTION


# endregion


# region Absence Ledger Tests
def test_vacation_quota_defaults_and_override() -> None:
    """Verify vacation quota storage and fallback to 30.0 days."""
    conn = sqlite3.connect(":memory:")
    ledger = AbsenceLedger(conn)
    default_quota = ledger.get_quota(YEAR_2025)
    assert isinstance(default_quota, VacationQuota)
    assert default_quota.entitlement_days == DEFAULT_ANNUAL_VACATION_DAYS
    assert default_quota.carryover_days == 0.0
    assert default_quota.total_available_days == DEFAULT_ANNUAL_VACATION_DAYS
    assert default_quota.to_dict()["total_available_days"] == DEFAULT_ANNUAL_VACATION_DAYS

    ledger.set_quota(
        YEAR_2025,
        entitlement_days=CUSTOM_ENTITLEMENT_DAYS,
        carryover_days=CUSTOM_CARRYOVER_DAYS,
    )
    stored_quota = ledger.get_quota(YEAR_2025)
    assert stored_quota.entitlement_days == CUSTOM_ENTITLEMENT_DAYS
    assert stored_quota.carryover_days == CUSTOM_CARRYOVER_DAYS
    assert stored_quota.total_available_days == TOTAL_CUSTOM_DAYS


def test_absence_crud_and_validation() -> None:
    """Verify absence recording, filtering, deletion, and validation guards."""
    with AbsenceLedger(":memory:") as ledger:
        with pytest.raises(ValueError, match=r"start_date .* cannot be after end_date"):
            ledger.record_absence(start_date="2025-05-10", end_date="2025-05-01")

        with pytest.raises(ValueError, match="Unknown absence kind"):
            ledger.record_absence(start_date="2025-05-01", kind="invalid_kind")

        with pytest.raises(ValueError, match="Invalid day_fraction"):
            ledger.record_absence(start_date="2025-05-01", day_fraction=INVALID_DAY_FRACTION)

        abs1 = ledger.record_absence(
            start_date="2025-05-05",
            end_date="2025-05-09",
            kind=ABSENCE_KIND_VACATION,
            notes="Spring break",
        )
        assert abs1.id.startswith("abs_")
        assert ledger.get_absence(abs1.id).notes == "Spring break"

        abs2 = ledger.record_absence(
            start_date="2025-05-12",
            end_date="2025-05-12",
            kind=ABSENCE_KIND_SICK_LEAVE,
            absence_id="sick_1",
        )
        assert abs2.id == "sick_1"

        all_abs = ledger.list_absences()
        assert len(all_abs) == 2
        vac_abs = ledger.list_absences(kind=ABSENCE_KIND_VACATION)
        assert len(vac_abs) == 1

        assert ledger.delete_absence("sick_1")
        assert not ledger.delete_absence("non_existent")
        with pytest.raises(KeyError):
            ledger.get_absence("sick_1")


def test_excluded_workplace_dates() -> None:
    """Verify excluded workplace dates aggregation with holidays and full day leave."""
    cal_de = HolidayCalendar(subdivision="DE")
    conn = sqlite3.connect(":memory:")
    ledger = AbsenceLedger(conn, calendar=cal_de)

    ledger.record_absence(
        start_date="2025-01-02",
        end_date="2025-01-03",
        kind=ABSENCE_KIND_VACATION,
        notes="New Year extended",
    )
    ledger.record_absence(
        start_date="2025-03-05",
        kind=ABSENCE_KIND_COMPENSATORY_OFF,
        day_fraction=HALF_DAY_FRACTION,
    )

    excluded = ledger.excluded_workplace_dates(
        "2025-01-01", "2025-01-05", include_public_holidays=True
    )
    assert "2025-01-01" in excluded  # Neujahr holiday
    assert "2025-01-02" in excluded  # Vacation day
    assert "2025-01-03" in excluded  # Vacation day

    excluded_half = ledger.excluded_workplace_dates("2025-03-05", "2025-03-05")
    assert "2025-03-05" not in excluded_half


def test_summarize_year() -> None:
    """Verify annual summary computation and remaining vacation balance."""
    cal_de = HolidayCalendar(subdivision="DE")
    conn = sqlite3.connect(":memory:")
    ledger = AbsenceLedger(conn, calendar=cal_de)

    ledger.set_quota(YEAR_2025, entitlement_days=30.0, carryover_days=2.0)
    absences_to_record = (
        ("2025-01-02", "2025-01-03", ABSENCE_KIND_VACATION, FULL_DAY_FRACTION),
        ("2025-02-03", "2025-02-03", ABSENCE_KIND_SICK_LEAVE, FULL_DAY_FRACTION),
        ("2025-08-04", "2025-08-08", ABSENCE_KIND_VACATION, FULL_DAY_FRACTION),
        ("2025-03-05", "2025-03-05", ABSENCE_KIND_COMPENSATORY_OFF, HALF_DAY_FRACTION),
        ("2025-03-06", "2025-03-06", ABSENCE_KIND_SPECIAL_LEAVE, FULL_DAY_FRACTION),
        ("2025-03-07", "2025-03-07", ABSENCE_KIND_UNPAID_LEAVE, FULL_DAY_FRACTION),
    )
    for start, end, kind, fraction in absences_to_record:
        ledger.record_absence(start_date=start, end_date=end, kind=kind, day_fraction=fraction)

    summary: VacationSummary = ledger.summarize_year(YEAR_2025, reference_date="2025-06-01")
    assert summary.year == YEAR_2025
    assert summary.subdivision == "DE"
    assert summary.entitlement_days == 30.0
    assert summary.carryover_days == 2.0
    assert summary.total_quota_days == 32.0
    assert summary.taken_vacation_days == 2.0
    assert summary.planned_vacation_days == 5.0
    assert summary.remaining_vacation_days == 25.0
    assert summary.sick_leave_days == 1.0
    assert summary.compensatory_off_days == HALF_DAY_FRACTION
    assert summary.special_leave_days == 1.0
    assert summary.unpaid_leave_days == 1.0
    assert summary.public_holiday_count == EXPECTED_NATIONWIDE_COUNT
    assert summary.weekday_public_holiday_count > 0
    assert summary.to_dict()["remaining_vacation_days"] == 25.0
# endregion
