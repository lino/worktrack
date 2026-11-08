"""SQLite-backed persistent store for business travel trips and itinerary data."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Self

from worktrack.reporting.travel import (
    ALLOWED_TRANSPORT_MODES,
    PER_DIEM_MIN_SINGLE_DAY_HOURS,
    ZERO_AMOUNT,
    DayMealFlags,
    TravelTrip,
)

# region Constants
CREATE_TABLE_SQL: Final = (
    "CREATE TABLE IF NOT EXISTS travel_trips ("
    "id TEXT PRIMARY KEY, title TEXT NOT NULL, destination TEXT NOT NULL, "
    "country_code TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT NOT NULL, "
    "transport_mode TEXT NOT NULL, distance_km REAL NOT NULL DEFAULT 0.0, "
    "meals_json TEXT NOT NULL DEFAULT '{}', entity_id TEXT, notes TEXT NOT NULL DEFAULT '', "
    "created_at TEXT NOT NULL); "
    "CREATE INDEX IF NOT EXISTS idx_travel_trips_bounds ON travel_trips (started_at, ended_at);"
)
INSERT_TRIP_SQL: Final = (
    "INSERT INTO travel_trips (id, title, destination, country_code, started_at, ended_at, "
    "transport_mode, distance_km, meals_json, entity_id, notes, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
SELECT_TRIP_SQL: Final = (
    "SELECT id, title, destination, country_code, started_at, ended_at, "
    "transport_mode, distance_km, meals_json, entity_id, notes, created_at FROM travel_trips"
)
# endregion


# region Persistence
class TravelTripStore:
    """SQLite-backed persistent store for business travel trips and itinerary data."""

    def __init__(self, conn_or_path: sqlite3.Connection | Path | str) -> None:
        """Initialize trip store from an active connection or filesystem path."""
        if isinstance(conn_or_path, sqlite3.Connection):
            self._conn = conn_or_path
            self._should_close = False
        else:
            self._conn = sqlite3.connect(str(conn_or_path))
            self._should_close = True
        self.ensure_schema()

    def close(self) -> None:
        """Close managed database connection if owned by the store."""
        if self._should_close:
            self._conn.close()

    def __enter__(self) -> Self:
        """Context manager entry returning self."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Context manager exit closing connection."""
        self.close()

    def ensure_schema(self) -> None:
        """Idempotently create travel trips tables and indexing structures."""
        self._conn.executescript(CREATE_TABLE_SQL)
        self._conn.commit()

    def record_trip(
        self,
        *,
        title: str,
        destination: str,
        started_at: str | datetime,
        ended_at: str | datetime,
        country_code: str = "DE",
        transport_mode: str = "train",
        distance_km: float = ZERO_AMOUNT,
        meals_by_date: Mapping[str, DayMealFlags | Mapping[str, bool]] | None = None,
        entity_id: str | None = None,
        notes: str = "",
        trip_id: str | None = None,
    ) -> TravelTrip:
        """Record and persist a new business travel trip."""
        mode = transport_mode.lower().strip()
        if mode not in ALLOWED_TRANSPORT_MODES:
            allowed = ", ".join(sorted(ALLOWED_TRANSPORT_MODES))
            raise ValueError(f"transport_mode must be one of {allowed}, got {mode!r}")

        t_id = trip_id or uuid.uuid4().hex
        start_iso = started_at.isoformat() if isinstance(started_at, datetime) else str(started_at)
        end_iso = ended_at.isoformat() if isinstance(ended_at, datetime) else str(ended_at)
        clean_meals = _normalize_meals(meals_by_date)

        trip = TravelTrip(
            id=t_id,
            title=title.strip(),
            destination=destination.strip(),
            country_code=country_code.upper().strip(),
            started_at=start_iso.strip(),
            ended_at=end_iso.strip(),
            transport_mode=mode,
            distance_km=max(ZERO_AMOUNT, float(distance_km)),
            meals_by_date=clean_meals,
            entity_id=entity_id.strip() if entity_id else None,
            notes=notes.strip(),
            created_at=datetime.now(UTC).isoformat(),
        )
        meals_json = json.dumps({k: v.to_dict() for k, v in clean_meals.items()})
        params = (
            trip.id, trip.title, trip.destination, trip.country_code,
            trip.started_at, trip.ended_at, trip.transport_mode, trip.distance_km,
            meals_json, trip.entity_id, trip.notes, trip.created_at,
        )
        self._conn.execute(INSERT_TRIP_SQL, params)
        self._conn.commit()
        return trip

    def get_trip(self, trip_id: str) -> TravelTrip:
        """Retrieve a trip by identifier, raising KeyError when not found."""
        row = self._conn.execute(
            f"{SELECT_TRIP_SQL} WHERE id = ?",
            (trip_id.strip(),),
        ).fetchone()
        if not row:
            raise KeyError(f"Trip '{trip_id}' not found.")
        return _row_to_trip(row)

    def list_trips(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[TravelTrip]:
        """List recorded trips overlapping an optional calendar date range."""
        clauses: list[str] = []
        params: list[object] = []
        if start_date is not None:
            clauses.append("substr(ended_at, 1, 10) >= ?")
            params.append(start_date.strip())
        if end_date is not None:
            clauses.append("substr(started_at, 1, 10) <= ?")
            params.append(end_date.strip())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"{SELECT_TRIP_SQL} {where} ORDER BY started_at ASC, id ASC"
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_trip(r) for r in rows]

    def delete_trip(self, trip_id: str) -> bool:
        """Delete a recorded trip by identifier."""
        cursor = self._conn.execute("DELETE FROM travel_trips WHERE id = ?", (trip_id.strip(),))
        self._conn.commit()
        return cursor.rowcount > 0

    def travel_dates_in_range(self, start_date: str, end_date: str) -> set[str]:
        """Collect full 24h or single-day >8h travel dates inside a date window."""
        dates: set[str] = set()
        trips = self.list_trips(start_date=start_date, end_date=end_date)
        for trip in trips:
            touched = trip.touched_dates()
            if len(touched) == 1 and trip.duration_hours() > PER_DIEM_MIN_SINGLE_DAY_HOURS:
                if start_date <= touched[0] <= end_date:
                    dates.add(touched[0])
            elif len(touched) >= 2:
                for day in touched[1:-1]:
                    if start_date <= day <= end_date:
                        dates.add(day)
        return dates


def _normalize_meals(
    meals: Mapping[str, DayMealFlags | Mapping[str, bool]] | None,
) -> dict[str, DayMealFlags]:
    """Normalize input meal specifications to `DayMealFlags` mapping."""
    clean: dict[str, DayMealFlags] = {}
    if not meals:
        return clean
    for d_str, flags in meals.items():
        if isinstance(flags, DayMealFlags):
            clean[d_str] = flags
        elif isinstance(flags, Mapping):
            clean[d_str] = DayMealFlags(
                breakfast=bool(flags.get("breakfast", False)),
                lunch=bool(flags.get("lunch", False)),
                dinner=bool(flags.get("dinner", False)),
            )
    return clean


def _row_to_trip(row: Sequence[object]) -> TravelTrip:
    """Convert a database tuple row to a `TravelTrip` instance."""
    raw_meals = str(row[8]) if row[8] else "{}"
    try:
        meals_map = json.loads(raw_meals)
    except (TypeError, json.JSONDecodeError):
        meals_map = {}

    clean_meals = {
        d: DayMealFlags(bool(f.get("breakfast")), bool(f.get("lunch")), bool(f.get("dinner")))
        for d, f in meals_map.items()
        if isinstance(f, dict)
    }

    return TravelTrip(
        id=str(row[0]),
        title=str(row[1]),
        destination=str(row[2]),
        country_code=str(row[3]),
        started_at=str(row[4]),
        ended_at=str(row[5]),
        transport_mode=str(row[6]),
        distance_km=float(str(row[7])) if row[7] is not None else ZERO_AMOUNT,
        meals_by_date=clean_meals,
        entity_id=str(row[9]) if row[9] is not None else None,
        notes=str(row[10]),
        created_at=str(row[11]),
    )


# endregion
