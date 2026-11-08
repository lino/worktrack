"""Statutory and custom holiday calendar models and calculations for German jurisdictions."""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Self

# region Constants
GERMAN_SUBDIVISIONS: tuple[str, ...] = (
    "DE-BW",
    "DE-BY",
    "DE-BE",
    "DE-BB",
    "DE-HB",
    "DE-HH",
    "DE-HE",
    "DE-MV",
    "DE-NI",
    "DE-NW",
    "DE-RP",
    "DE-SL",
    "DE-SN",
    "DE-ST",
    "DE-SH",
    "DE-TH",
)

OFFSET_KARFREITAG_DAYS = -2
OFFSET_OSTERMONTAG_DAYS = 1
OFFSET_CHRISTI_HIMMELFAHRT_DAYS = 39
OFFSET_PFINGSTMONTAG_DAYS = 50
OFFSET_FRONLEICHNAM_DAYS = 60

MONTH_JANUARY = 1
MONTH_MARCH = 3
MONTH_MAY = 5
MONTH_AUGUST = 8
MONTH_SEPTEMBER = 9
MONTH_OCTOBER = 10
MONTH_NOVEMBER = 11
MONTH_DECEMBER = 12

DAY_NEW_YEAR = 1
DAY_EPIPHANY = 6
DAY_WOMENS_DAY = 8
DAY_LABOR_DAY = 1
DAY_ASSUMPTION = 15
DAY_CHILDRENS_DAY = 20
DAY_GERMAN_UNITY = 3
DAY_REFORMATION = 31
DAY_ALL_SAINTS = 1
DAY_CHRISTMAS_DAY = 25
DAY_BOXING_DAY = 26
DAY_BUSS_UND_BETTAG_ANCHOR = 23

WEDNESDAY_WEEKDAY_INDEX = 2
SATURDAY_WEEKDAY_INDEX = 5
DAYS_IN_WEEK = 7
# endregion


# region Easter & Date Algorithms
def compute_easter_sunday(year: int) -> date:
    """Compute Gregorian Easter Sunday using the Butcher-Meeus algorithm.

    Args:
        year: The Gregorian calendar year.

    Returns:
        The calculated Easter Sunday date.
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l_val = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_val) // 451
    month = (h + l_val - 7 * m + 114) // 31
    day = ((h + l_val - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def compute_buss_und_bettag(year: int) -> date:
    """Compute Buß- und Bettag (Wednesday before Nov 23) for a given year.

    Args:
        year: Gregorian calendar year.

    Returns:
        Date of Buß- und Bettag in November.
    """
    nov_anchor = date(year, MONTH_NOVEMBER, DAY_BUSS_UND_BETTAG_ANCHOR)
    offset = (nov_anchor.weekday() - WEDNESDAY_WEEKDAY_INDEX) % DAYS_IN_WEEK
    days_back = DAYS_IN_WEEK if offset == 0 else offset
    return nov_anchor - timedelta(days=days_back)


NATIONWIDE_RULES: tuple[tuple[str, Callable[[int, date], date]], ...] = (
    ("Neujahr", lambda y, _: date(y, MONTH_JANUARY, DAY_NEW_YEAR)),
    ("Karfreitag", lambda _, e: e + timedelta(days=OFFSET_KARFREITAG_DAYS)),
    ("Ostermontag", lambda _, e: e + timedelta(days=OFFSET_OSTERMONTAG_DAYS)),
    ("Tag der Arbeit", lambda y, _: date(y, MONTH_MAY, DAY_LABOR_DAY)),
    ("Christi Himmelfahrt", lambda _, e: e + timedelta(days=OFFSET_CHRISTI_HIMMELFAHRT_DAYS)),
    ("Pfingstmontag", lambda _, e: e + timedelta(days=OFFSET_PFINGSTMONTAG_DAYS)),
    ("Tag der Deutschen Einheit", lambda y, _: date(y, MONTH_OCTOBER, DAY_GERMAN_UNITY)),
    ("1. Weihnachtstag", lambda y, _: date(y, MONTH_DECEMBER, DAY_CHRISTMAS_DAY)),
    ("2. Weihnachtstag", lambda y, _: date(y, MONTH_DECEMBER, DAY_BOXING_DAY)),
)

REGIONAL_RULES: tuple[tuple[str, Callable[[int, date], date], tuple[str, ...]], ...] = (
    (
        "Heilige Drei Könige",
        lambda y, _: date(y, MONTH_JANUARY, DAY_EPIPHANY),
        ("DE-BW", "DE-BY", "DE-ST"),
    ),
    (
        "Internationaler Frauentag",
        lambda y, _: date(y, MONTH_MARCH, DAY_WOMENS_DAY),
        ("DE-BE", "DE-MV"),
    ),
    (
        "Fronleichnam",
        lambda _, e: e + timedelta(days=OFFSET_FRONLEICHNAM_DAYS),
        ("DE-BW", "DE-BY", "DE-HE", "DE-NW", "DE-RP", "DE-SL"),
    ),
    ("Mariä Himmelfahrt", lambda y, _: date(y, MONTH_AUGUST, DAY_ASSUMPTION), ("DE-BY", "DE-SL")),
    ("Weltkindertag", lambda y, _: date(y, MONTH_SEPTEMBER, DAY_CHILDRENS_DAY), ("DE-TH",)),
    (
        "Reformationstag",
        lambda y, _: date(y, MONTH_OCTOBER, DAY_REFORMATION),
        ("DE-BB", "DE-HB", "DE-HH", "DE-MV", "DE-NI", "DE-SN", "DE-ST", "DE-SH", "DE-TH"),
    ),
    (
        "Allerheiligen",
        lambda y, _: date(y, MONTH_NOVEMBER, DAY_ALL_SAINTS),
        ("DE-BW", "DE-BY", "DE-NW", "DE-RP", "DE-SL"),
    ),
    ("Buß- und Bettag", lambda y, _: compute_buss_und_bettag(y), ("DE-SN",)),
)
# endregion


# region PublicHoliday Model
@dataclass(frozen=True, slots=True)
class PublicHoliday:
    """Represents a statutory or custom public holiday."""

    day: date
    name: str
    subdivision: str
    is_custom: bool = False

    def to_dict(self) -> dict[str, object]:
        """Serialize holiday to dictionary format.

        Returns:
            Dictionary containing date, name, subdivision, and is_custom flag.
        """
        return {
            "date": self.day.isoformat(),
            "name": self.name,
            "subdivision": self.subdivision,
            "is_custom": self.is_custom,
        }
# endregion


# region HolidayCalendar Class
class HolidayCalendar:
    """Evaluates public holidays and workdays across German jurisdictions and custom entries."""

    def __init__(
        self,
        subdivision: str = "",
        custom_holidays: Mapping[str, str] | Sequence[PublicHoliday] | None = None,
    ) -> None:
        """Initialize holiday calendar for subdivision with optional custom holidays.

        Args:
            subdivision: Empty string for no statutory holidays, 'DE' for nationwide
                federal holidays, or one of GERMAN_SUBDIVISIONS.
            custom_holidays: Optional date-to-name mapping or PublicHoliday sequence.

        Raises:
            ValueError: If subdivision is not recognized.
            TypeError: If custom_holidays format is unsupported.
        """
        valid_subdivisions = {"", "DE", *GERMAN_SUBDIVISIONS}
        if subdivision not in valid_subdivisions:
            raise ValueError(
                f"Unknown subdivision code: {subdivision!r}. Must be '' or 'DE' "
                f"or one of {GERMAN_SUBDIVISIONS}."
            )
        self._subdivision = subdivision
        self._custom_holidays: dict[date, PublicHoliday] = {}
        if custom_holidays is not None:
            self._register_custom_holidays(custom_holidays)
        self._year_cache: dict[int, dict[date, PublicHoliday]] = {}

    @property
    def subdivision(self) -> str:
        """Return the active jurisdiction subdivision code."""
        return self._subdivision

    @classmethod
    def from_toml(cls, source: str | Path, *, subdivision: str | None = None) -> Self:
        """Load a HolidayCalendar from a TOML configuration file or text.

        Args:
            source: Path to TOML file or raw TOML string.
            subdivision: Optional subdivision override.

        Returns:
            Configured HolidayCalendar instance.
        """
        content: str
        if isinstance(source, Path) or (
            isinstance(source, str) and "\n" not in source and Path(source).exists()
        ):
            content = Path(source).read_text(encoding="utf-8")
        else:
            content = source
        data = tomllib.loads(content)
        selected_subdivision = (
            subdivision if subdivision is not None else str(data.get("subdivision", ""))
        )
        raw_holidays = data.get("holidays", {})
        mapping: dict[str, str] = {}
        if isinstance(raw_holidays, dict):
            mapping = {str(k): str(v) for k, v in raw_holidays.items()}
        return cls(subdivision=selected_subdivision, custom_holidays=mapping)

    def _register_custom_holidays(
        self, custom_holidays: Mapping[str, str] | Sequence[PublicHoliday]
    ) -> None:
        """Populate custom holidays from mapping or sequence."""
        if isinstance(custom_holidays, Mapping):
            for date_str, name in custom_holidays.items():
                parsed = date.fromisoformat(date_str)
                self._custom_holidays[parsed] = PublicHoliday(
                    day=parsed,
                    name=str(name),
                    subdivision=self._subdivision or "CUSTOM",
                    is_custom=True,
                )
        elif isinstance(custom_holidays, Sequence):
            for item in custom_holidays:
                self._custom_holidays[item.day] = item
        else:
            raise TypeError(
                f"custom_holidays must be a Mapping or Sequence, got {type(custom_holidays)}"
            )

    def _normalize_date(self, day: date | str) -> date:
        """Normalize string or date into a date object."""
        if isinstance(day, date):
            return day.date() if isinstance(day, datetime) else day
        if isinstance(day, str):
            return date.fromisoformat(day)
        raise TypeError(f"Expected date or str, got {type(day)}")

    def _get_year_holidays_map(self, year: int) -> dict[date, PublicHoliday]:
        """Fetch or calculate the holiday lookup dictionary for a specific year."""
        if year in self._year_cache:
            return self._year_cache[year]

        holidays_map: dict[date, PublicHoliday] = {}
        if self._subdivision:
            easter = compute_easter_sunday(year)
            for name, date_fn in NATIONWIDE_RULES:
                h_date = date_fn(year, easter)
                holidays_map[h_date] = PublicHoliday(
                    day=h_date,
                    name=name,
                    subdivision=self._subdivision,
                    is_custom=False,
                )
            if self._subdivision in GERMAN_SUBDIVISIONS:
                for name, date_fn, subdivisions in REGIONAL_RULES:
                    if self._subdivision in subdivisions:
                        h_date = date_fn(year, easter)
                        holidays_map[h_date] = PublicHoliday(
                            day=h_date,
                            name=name,
                            subdivision=self._subdivision,
                            is_custom=False,
                        )

        for day, custom_holiday in self._custom_holidays.items():
            if day.year == year:
                holidays_map[day] = custom_holiday

        self._year_cache[year] = holidays_map
        return holidays_map

    def holidays_for_year(self, year: int) -> list[PublicHoliday]:
        """Return all statutory and custom public holidays for a year, sorted by date.

        Args:
            year: Calendar year to query.

        Returns:
            Sorted list of PublicHoliday objects.
        """
        return sorted(self._get_year_holidays_map(year).values(), key=lambda h: h.day)

    def holidays_in_range(
        self, start_date: date | str, end_date: date | str
    ) -> list[PublicHoliday]:
        """Return all public holidays falling within the given inclusive range.

        Args:
            start_date: Start of range (inclusive).
            end_date: End of range (inclusive).

        Returns:
            Sorted list of PublicHoliday objects within range.
        """
        start = self._normalize_date(start_date)
        end = self._normalize_date(end_date)
        if start > end:
            return []
        result: list[PublicHoliday] = []
        for year in range(start.year, end.year + 1):
            for holiday in self.holidays_for_year(year):
                if start <= holiday.day <= end:
                    result.append(holiday)
        return result

    def is_public_holiday(self, day: date | str) -> bool:
        """Return True if the date is a recognized public holiday in this calendar.

        Args:
            day: Date or ISO string to check.

        Returns:
            True if date is a public holiday, otherwise False.
        """
        return self.holiday_name(day) is not None

    def holiday_name(self, day: date | str) -> str | None:
        """Return the public holiday name if the date is a holiday, otherwise None.

        Args:
            day: Date or ISO string to inspect.

        Returns:
            Holiday name string or None.
        """
        parsed = self._normalize_date(day)
        holiday = self._get_year_holidays_map(parsed.year).get(parsed)
        return holiday.name if holiday is not None else None

    def is_workday(self, day: date | str) -> bool:
        """Return True iff the date falls on Monday..Friday and is not a public holiday.

        Args:
            day: Date or ISO string to evaluate.

        Returns:
            True if date is an active working day, otherwise False.
        """
        parsed = self._normalize_date(day)
        return parsed.weekday() < SATURDAY_WEEKDAY_INDEX and not self.is_public_holiday(parsed)
# endregion
