"""Absence domain models, vacation quotas, and summary representations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from worktrack.absences.holidays import HolidayCalendar

# region Constants
ABSENCE_KIND_VACATION = "vacation"
ABSENCE_KIND_SICK_LEAVE = "sick_leave"
ABSENCE_KIND_COMPENSATORY_OFF = "compensatory_off"
ABSENCE_KIND_SPECIAL_LEAVE = "special_leave"
ABSENCE_KIND_UNPAID_LEAVE = "unpaid_leave"

ABSENCE_KINDS = frozenset(
    {
        ABSENCE_KIND_VACATION,
        ABSENCE_KIND_SICK_LEAVE,
        ABSENCE_KIND_COMPENSATORY_OFF,
        ABSENCE_KIND_SPECIAL_LEAVE,
        ABSENCE_KIND_UNPAID_LEAVE,
    }
)

ALLOWED_DAY_FRACTIONS = frozenset({0.5, 1.0})
DEFAULT_ANNUAL_VACATION_DAYS = 30.0
SATURDAY_WEEKDAY_INDEX = 5
# endregion


# region AbsenceEntry Dataclass
@dataclass(frozen=True, slots=True)
class AbsenceEntry:
    """Represents a scheduled or taken leave/absence record."""

    id: str
    start_date: str
    end_date: str
    kind: str
    day_fraction: float
    notes: str
    created_at: str

    def workdays_covered(self, calendar: HolidayCalendar | None = None) -> list[str]:
        """Return all ISO date strings within the absence range that are working days.

        Args:
            calendar: Optional holiday calendar. If provided, checks against the
                calendar's workday rules; otherwise considers Monday through Friday
                as workdays.

        Returns:
            Sorted list of date strings in 'YYYY-MM-DD' format.
        """
        start = date.fromisoformat(self.start_date)
        end = date.fromisoformat(self.end_date)
        current = start
        workdays: list[str] = []
        one_day = timedelta(days=1)
        while current <= end:
            if calendar is not None:
                if calendar.is_workday(current):
                    workdays.append(current.isoformat())
            elif current.weekday() < SATURDAY_WEEKDAY_INDEX:
                workdays.append(current.isoformat())
            current += one_day
        return workdays

    def effective_days(self, calendar: HolidayCalendar | None = None) -> float:
        """Calculate effective absent working days adjusted for day fraction.

        Args:
            calendar: Optional holiday calendar used to evaluate workdays.

        Returns:
            Number of effective absent working days.
        """
        return len(self.workdays_covered(calendar)) * self.day_fraction

    def to_dict(self, calendar: HolidayCalendar | None = None) -> dict[str, object]:
        """Serialize absence entry to dictionary format.

        Args:
            calendar: Optional calendar to compute workday coverage.

        Returns:
            Dictionary containing absence attributes and computed working days.
        """
        return {
            "id": self.id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "kind": self.kind,
            "day_fraction": self.day_fraction,
            "notes": self.notes,
            "created_at": self.created_at,
            "workdays_covered": self.workdays_covered(calendar),
            "effective_days": self.effective_days(calendar),
        }
# endregion


# region VacationQuota Dataclass
@dataclass(frozen=True, slots=True)
class VacationQuota:
    """Represents an annual vacation allowance and carryover."""

    year: int
    entitlement_days: float
    carryover_days: float
    updated_at: str

    @property
    def total_available_days(self) -> float:
        """Return the sum of entitlement and carryover days."""
        return self.entitlement_days + self.carryover_days

    def to_dict(self) -> dict[str, object]:
        """Serialize vacation quota to dictionary format.

        Returns:
            Dictionary containing quota attributes.
        """
        return {
            "year": self.year,
            "entitlement_days": self.entitlement_days,
            "carryover_days": self.carryover_days,
            "total_available_days": self.total_available_days,
            "updated_at": self.updated_at,
        }
# endregion


# region VacationSummary Dataclass
@dataclass(frozen=True, slots=True)
class VacationSummary:
    """Consolidated annual leave breakdown and quota balance."""

    year: int
    subdivision: str
    entitlement_days: float
    carryover_days: float
    total_quota_days: float
    taken_vacation_days: float
    planned_vacation_days: float
    remaining_vacation_days: float
    sick_leave_days: float
    compensatory_off_days: float
    special_leave_days: float
    unpaid_leave_days: float
    public_holiday_count: int
    weekday_public_holiday_count: int

    def to_dict(self) -> dict[str, object]:
        """Serialize summary to dictionary format.

        Returns:
            Dictionary containing annual metrics and remaining vacation.
        """
        return {
            "year": self.year,
            "subdivision": self.subdivision,
            "entitlement_days": self.entitlement_days,
            "carryover_days": self.carryover_days,
            "total_quota_days": self.total_quota_days,
            "taken_vacation_days": self.taken_vacation_days,
            "planned_vacation_days": self.planned_vacation_days,
            "remaining_vacation_days": self.remaining_vacation_days,
            "sick_leave_days": self.sick_leave_days,
            "compensatory_off_days": self.compensatory_off_days,
            "special_leave_days": self.special_leave_days,
            "unpaid_leave_days": self.unpaid_leave_days,
            "public_holiday_count": self.public_holiday_count,
            "weekday_public_holiday_count": self.weekday_public_holiday_count,
        }
# endregion


# region Aggregation Helpers
def aggregate_absence_days(
    entries: list[AbsenceEntry],
    year: int,
    ref_date: date,
    calendar: HolidayCalendar,
) -> tuple[float, float, float, float, float, float]:
    """Aggregate vacation and other absence category days for the given year.

    Args:
        entries: Filtered absence entries overlapping the year.
        year: Target calendar year.
        ref_date: Date dividing taken from planned vacation.
        calendar: Holiday calendar for workday determinations.

    Returns:
        Tuple of (taken_vac, planned_vac, sick, comp_off, spec_leave, unpaid_leave).
    """
    year_prefix = f"{year:04d}-"
    taken_vac = 0.0
    planned_vac = 0.0
    sick = 0.0
    comp_off = 0.0
    spec_leave = 0.0
    unpaid_leave = 0.0

    for entry in entries:
        workdays = entry.workdays_covered(calendar)
        for w_str in workdays:
            if not w_str.startswith(year_prefix):
                continue
            w_date = date.fromisoformat(w_str)
            if entry.kind == ABSENCE_KIND_VACATION:
                if w_date <= ref_date:
                    taken_vac += entry.day_fraction
                else:
                    planned_vac += entry.day_fraction
            elif entry.kind == ABSENCE_KIND_SICK_LEAVE:
                sick += entry.day_fraction
            elif entry.kind == ABSENCE_KIND_COMPENSATORY_OFF:
                comp_off += entry.day_fraction
            elif entry.kind == ABSENCE_KIND_SPECIAL_LEAVE:
                spec_leave += entry.day_fraction
            elif entry.kind == ABSENCE_KIND_UNPAID_LEAVE:
                unpaid_leave += entry.day_fraction

    return taken_vac, planned_vac, sick, comp_off, spec_leave, unpaid_leave
# endregion

