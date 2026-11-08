"""Absences, statutory holiday calendars, and vacation quota ledger."""

from __future__ import annotations

from worktrack.absences.holidays import (
    GERMAN_SUBDIVISIONS,
    HolidayCalendar,
    PublicHoliday,
)
from worktrack.absences.ledger import (
    ABSENCE_KIND_COMPENSATORY_OFF,
    ABSENCE_KIND_SICK_LEAVE,
    ABSENCE_KIND_SPECIAL_LEAVE,
    ABSENCE_KIND_UNPAID_LEAVE,
    ABSENCE_KIND_VACATION,
    ABSENCE_KINDS,
    ALLOWED_DAY_FRACTIONS,
    DEFAULT_ANNUAL_VACATION_DAYS,
    AbsenceEntry,
    AbsenceLedger,
    VacationQuota,
    VacationSummary,
)

__all__ = [
    "ABSENCE_KINDS",
    "ABSENCE_KIND_COMPENSATORY_OFF",
    "ABSENCE_KIND_SICK_LEAVE",
    "ABSENCE_KIND_SPECIAL_LEAVE",
    "ABSENCE_KIND_UNPAID_LEAVE",
    "ABSENCE_KIND_VACATION",
    "ALLOWED_DAY_FRACTIONS",
    "DEFAULT_ANNUAL_VACATION_DAYS",
    "GERMAN_SUBDIVISIONS",
    "AbsenceEntry",
    "AbsenceLedger",
    "HolidayCalendar",
    "PublicHoliday",
    "VacationQuota",
    "VacationSummary",
]
