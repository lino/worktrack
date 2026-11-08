"""Absence ledger SQLite persistence and vacation calculation engine."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Self

from worktrack.absences.holidays import HolidayCalendar
from worktrack.absences.models import (
    ABSENCE_KIND_COMPENSATORY_OFF,
    ABSENCE_KIND_SICK_LEAVE,
    ABSENCE_KIND_SPECIAL_LEAVE,
    ABSENCE_KIND_UNPAID_LEAVE,
    ABSENCE_KIND_VACATION,
    ABSENCE_KINDS,
    ALLOWED_DAY_FRACTIONS,
    DEFAULT_ANNUAL_VACATION_DAYS,
    AbsenceEntry,
    VacationQuota,
    VacationSummary,
    aggregate_absence_days,
)

# region Constants
FULL_DAY_FRACTION = 1.0
SATURDAY_WEEKDAY_INDEX = 5
ID_HEX_LENGTH = 12
# endregion


# region Absence Ledger Class
class AbsenceLedger:
    """Manages absence entries, annual quotas, and public holiday intersections."""

    def __init__(
        self,
        conn_or_path: sqlite3.Connection | Path | str,
        *,
        calendar: HolidayCalendar | None = None,
    ) -> None:
        """Initialize absence ledger with connection or filesystem path.

        Args:
            conn_or_path: Active SQLite connection, Path, or connection string.
            calendar: Optional holiday calendar for workday filtering.
        """
        if isinstance(conn_or_path, sqlite3.Connection):
            self._conn: sqlite3.Connection | None = conn_or_path
            self._owns_conn = False
        else:
            self._conn = sqlite3.connect(str(conn_or_path))
            self._owns_conn = True
        self._calendar = calendar if calendar is not None else HolidayCalendar()
        self.ensure_schema()

    @property
    def calendar(self) -> HolidayCalendar:
        """Return the active holiday calendar."""
        return self._calendar

    def close(self) -> None:
        """Close connection if owned by this instance."""
        if self._owns_conn and self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Exit context manager, closing connection if owned."""
        self.close()

    def _require_conn(self) -> sqlite3.Connection:
        """Return the open connection or raise RuntimeError."""
        if self._conn is None:
            raise RuntimeError("AbsenceLedger connection is closed.")
        return self._conn

    def ensure_schema(self) -> None:
        """Create vacation_quotas and absences tables idempotently."""
        conn = self._require_conn()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vacation_quotas (
                    year INTEGER PRIMARY KEY,
                    entitlement_days REAL NOT NULL,
                    carryover_days REAL NOT NULL DEFAULT 0.0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS absences (
                    id TEXT PRIMARY KEY,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    day_fraction REAL NOT NULL DEFAULT 1.0,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_absences_dates ON absences(start_date, end_date);
                CREATE INDEX IF NOT EXISTS idx_absences_kind ON absences(kind);
                """
            )

    def set_quota(
        self, year: int, entitlement_days: float, carryover_days: float = 0.0
    ) -> VacationQuota:
        """Create or update the annual vacation quota for a year.

        Args:
            year: Calendar year.
            entitlement_days: Standard entitlement for the year.
            carryover_days: Days brought forward from previous year.

        Returns:
            Saved VacationQuota instance.
        """
        conn = self._require_conn()
        updated_at = datetime.now(UTC).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO vacation_quotas (year, entitlement_days, carryover_days, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(year) DO UPDATE SET
                    entitlement_days = excluded.entitlement_days,
                    carryover_days = excluded.carryover_days,
                    updated_at = excluded.updated_at
                """,
                (year, float(entitlement_days), float(carryover_days), updated_at),
            )
        return VacationQuota(
            year=year,
            entitlement_days=float(entitlement_days),
            carryover_days=float(carryover_days),
            updated_at=updated_at,
        )

    def get_quota(self, year: int) -> VacationQuota:
        """Retrieve stored vacation quota or default to 30.0 days.

        Args:
            year: Calendar year to query.

        Returns:
            VacationQuota for the year.
        """
        conn = self._require_conn()
        cursor = conn.execute(
            "SELECT year, entitlement_days, carryover_days, updated_at "
            "FROM vacation_quotas WHERE year = ?",
            (year,),
        )
        row = cursor.fetchone()
        if row is not None:
            return VacationQuota(
                year=int(row[0]),
                entitlement_days=float(row[1]),
                carryover_days=float(row[2]),
                updated_at=str(row[3]),
            )
        return VacationQuota(
            year=year,
            entitlement_days=DEFAULT_ANNUAL_VACATION_DAYS,
            carryover_days=0.0,
            updated_at="",
        )

    def record_absence(
        self,
        *,
        start_date: str | date,
        end_date: str | date | None = None,
        kind: str = ABSENCE_KIND_VACATION,
        day_fraction: float = 1.0,
        notes: str = "",
        absence_id: str | None = None,
    ) -> AbsenceEntry:
        """Record an absence entry in the ledger.

        Args:
            start_date: Absence start date.
            end_date: Absence end date (defaults to start_date if omitted).
            kind: Absence type from ABSENCE_KINDS.
            day_fraction: Fractional workday value (0.5 or 1.0).
            notes: Optional descriptive remarks.
            absence_id: Optional explicit identifier.

        Returns:
            Created AbsenceEntry.

        Raises:
            ValueError: If start_date > end_date, kind is unknown, or day_fraction is invalid.
        """
        s_date = start_date.isoformat() if isinstance(start_date, date) else str(start_date)
        e_date = (
            end_date.isoformat()
            if isinstance(end_date, date)
            else (s_date if end_date is None else str(end_date))
        )
        if s_date > e_date:
            raise ValueError(f"start_date ({s_date}) cannot be after end_date ({e_date})")
        if kind not in ABSENCE_KINDS:
            kinds_list = sorted(ABSENCE_KINDS)
            raise ValueError(f"Unknown absence kind: {kind!r}. Must be one of {kinds_list}")
        if day_fraction not in ALLOWED_DAY_FRACTIONS:
            fractions_list = sorted(ALLOWED_DAY_FRACTIONS)
            raise ValueError(f"Invalid day_fraction: {day_fraction}. Must be in {fractions_list}")

        entry_id = absence_id or f"abs_{uuid.uuid4().hex[:ID_HEX_LENGTH]}"
        created_at = datetime.now(UTC).isoformat()
        conn = self._require_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO absences (
                    id, start_date, end_date, kind, day_fraction, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_id, s_date, e_date, kind, float(day_fraction), notes, created_at),
            )
        return AbsenceEntry(
            id=entry_id,
            start_date=s_date,
            end_date=e_date,
            kind=kind,
            day_fraction=float(day_fraction),
            notes=notes,
            created_at=created_at,
        )

    def get_absence(self, absence_id: str) -> AbsenceEntry:
        """Fetch an absence entry by identifier.

        Args:
            absence_id: Unique record ID.

        Returns:
            Matching AbsenceEntry.

        Raises:
            KeyError: If record does not exist.
        """
        conn = self._require_conn()
        cursor = conn.execute(
            "SELECT id, start_date, end_date, kind, day_fraction, notes, created_at "
            "FROM absences WHERE id = ?",
            (absence_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(f"Absence entry not found: {absence_id}")
        return AbsenceEntry(
            id=str(row[0]),
            start_date=str(row[1]),
            end_date=str(row[2]),
            kind=str(row[3]),
            day_fraction=float(row[4]),
            notes=str(row[5]),
            created_at=str(row[6]),
        )

    def list_absences(
        self,
        *,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        kind: str | None = None,
    ) -> list[AbsenceEntry]:
        """List absence entries overlapping the given bounds, sorted by start_date ASC, id ASC.

        Args:
            start_date: Optional lower bound (inclusive overlap).
            end_date: Optional upper bound (inclusive overlap).
            kind: Optional absence category filter.

        Returns:
            List of matching AbsenceEntry objects.
        """
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[object] = []
        if start_date is not None:
            s_str = start_date.isoformat() if isinstance(start_date, date) else str(start_date)
            clauses.append("end_date >= ?")
            params.append(s_str)
        if end_date is not None:
            e_str = end_date.isoformat() if isinstance(end_date, date) else str(end_date)
            clauses.append("start_date <= ?")
            params.append(e_str)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT id, start_date, end_date, kind, day_fraction, notes, created_at "
            f"FROM absences {where} ORDER BY start_date ASC, id ASC"
        )
        cursor = conn.execute(sql, params)
        return [
            AbsenceEntry(
                id=str(row[0]),
                start_date=str(row[1]),
                end_date=str(row[2]),
                kind=str(row[3]),
                day_fraction=float(row[4]),
                notes=str(row[5]),
                created_at=str(row[6]),
            )
            for row in cursor.fetchall()
        ]

    def delete_absence(self, absence_id: str) -> bool:
        """Delete an absence record by identifier.

        Args:
            absence_id: Unique record ID.

        Returns:
            True if an entry was deleted, False otherwise.
        """
        conn = self._require_conn()
        with conn:
            cursor = conn.execute("DELETE FROM absences WHERE id = ?", (absence_id,))
            return cursor.rowcount > 0

    def excluded_workplace_dates(
        self,
        start_date: str | date,
        end_date: str | date,
        *,
        include_public_holidays: bool = True,
    ) -> set[str]:
        """Return dates where workplace presence is excluded due to full absences or holidays.

        Args:
            start_date: Start of date range.
            end_date: End of date range.
            include_public_holidays: Whether to include public holidays.

        Returns:
            Set of ISO date strings for full-day excluded dates.
        """
        s_str = start_date.isoformat() if isinstance(start_date, date) else str(start_date)
        e_str = end_date.isoformat() if isinstance(end_date, date) else str(end_date)
        entries = self.list_absences(start_date=s_str, end_date=e_str)
        excluded: set[str] = set()
        for entry in entries:
            if entry.day_fraction == FULL_DAY_FRACTION:
                for day_str in entry.workdays_covered(self._calendar):
                    if s_str <= day_str <= e_str:
                        excluded.add(day_str)
        if include_public_holidays and self._calendar is not None:
            for holiday in self._calendar.holidays_in_range(s_str, e_str):
                excluded.add(holiday.day.isoformat())
        return excluded

    def summarize_year(
        self, year: int, *, reference_date: date | str | None = None
    ) -> VacationSummary:
        """Compute consolidated vacation balance and absence days for a year.

        Args:
            year: Calendar year to summarize.
            reference_date: Cutoff date separating taken from planned vacation. Defaults to today.

        Returns:
            VacationSummary populated with quotas, taken/planned leave, and holiday counts.
        """
        ref_date = (
            date.today()
            if reference_date is None
            else (
                date.fromisoformat(reference_date)
                if isinstance(reference_date, str)
                else reference_date
            )
        )
        year_start = f"{year:04d}-01-01"
        year_end = f"{year:04d}-12-31"
        quota = self.get_quota(year)

        entries = self.list_absences(start_date=year_start, end_date=year_end)
        (
            taken_vac,
            planned_vac,
            sick,
            comp_off,
            spec_leave,
            unpaid_leave,
        ) = aggregate_absence_days(entries, year, ref_date, self._calendar)

        total_quota = quota.total_available_days
        remaining_vac = total_quota - taken_vac - planned_vac

        year_holidays = self._calendar.holidays_for_year(year)
        holiday_count = len(year_holidays)
        weekday_holidays = sum(1 for h in year_holidays if h.day.weekday() < SATURDAY_WEEKDAY_INDEX)

        return VacationSummary(
            year=year,
            subdivision=self._calendar.subdivision,
            entitlement_days=quota.entitlement_days,
            carryover_days=quota.carryover_days,
            total_quota_days=total_quota,
            taken_vacation_days=taken_vac,
            planned_vacation_days=planned_vac,
            remaining_vacation_days=remaining_vac,
            sick_leave_days=sick,
            compensatory_off_days=comp_off,
            special_leave_days=spec_leave,
            unpaid_leave_days=unpaid_leave,
            public_holiday_count=holiday_count,
            weekday_public_holiday_count=weekday_holidays,
        )


# endregion


__all__ = [
    "ABSENCE_KINDS",
    "ABSENCE_KIND_COMPENSATORY_OFF",
    "ABSENCE_KIND_SICK_LEAVE",
    "ABSENCE_KIND_SPECIAL_LEAVE",
    "ABSENCE_KIND_UNPAID_LEAVE",
    "ABSENCE_KIND_VACATION",
    "ALLOWED_DAY_FRACTIONS",
    "DEFAULT_ANNUAL_VACATION_DAYS",
    "AbsenceEntry",
    "AbsenceLedger",
    "VacationQuota",
    "VacationSummary",
]
