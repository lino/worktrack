"""Business travel models, statutory per-diem calculations, and SQLite trip store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from worktrack.reporting.packs import ReportingPack

if TYPE_CHECKING:
    from worktrack.reporting.store import TravelTripStore

# region Constants
TRANSPORT_MODE_CAR: Final = "car"
TRANSPORT_MODE_TRAIN: Final = "train"
TRANSPORT_MODE_FLIGHT: Final = "flight"
TRANSPORT_MODE_PUBLIC_TRANSIT: Final = "public_transit"
TRANSPORT_MODE_OTHER: Final = "other"

ALLOWED_TRANSPORT_MODES: Final[frozenset[str]] = frozenset(
    {
        TRANSPORT_MODE_CAR,
        TRANSPORT_MODE_TRAIN,
        TRANSPORT_MODE_FLIGHT,
        TRANSPORT_MODE_PUBLIC_TRANSIT,
        TRANSPORT_MODE_OTHER,
    }
)

DAY_CLASS_SINGLE_UNDER_8H: Final = "single_day_under_8h"
DAY_CLASS_SINGLE_OVER_8H: Final = "single_day_over_8h"
DAY_CLASS_ARRIVAL_OR_DEPARTURE: Final = "arrival_or_departure"
DAY_CLASS_FULL_24H: Final = "full_24h_day"

DECIMAL_PLACES: Final = 2
ZERO_AMOUNT: Final = 0.0
SECONDS_PER_HOUR: Final = 3600.0
DATE_SLICE_LEN: Final = 10
PER_DIEM_MIN_SINGLE_DAY_HOURS: Final = 8.0
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class DayMealFlags:
    """Meal provision indicators for statutory per-diem reduction calculations."""

    breakfast: bool = False
    lunch: bool = False
    dinner: bool = False

    def to_dict(self) -> dict[str, bool]:
        """Serialize meal flags to a dictionary."""
        return {"breakfast": self.breakfast, "lunch": self.lunch, "dinner": self.dinner}


@dataclass(frozen=True, slots=True)
class TravelTrip:
    """Recorded business travel trip with schedule, route, and employer meals."""

    id: str
    title: str
    destination: str
    country_code: str
    started_at: str
    ended_at: str
    transport_mode: str
    distance_km: float
    meals_by_date: Mapping[str, DayMealFlags]
    entity_id: str | None
    notes: str
    created_at: str

    def touched_dates(self) -> list[str]:
        """Compute chronological list of all calendar dates spanned by the trip."""
        start_d = date.fromisoformat(self.started_at[:DATE_SLICE_LEN])
        end_d = date.fromisoformat(self.ended_at[:DATE_SLICE_LEN])
        dates: list[str] = []
        curr = start_d
        while curr <= end_d:
            dates.append(curr.isoformat())
            curr += timedelta(days=1)
        return dates

    def duration_hours(self) -> float:
        """Calculate total trip duration in decimal hours."""
        start_dt = datetime.fromisoformat(self.started_at)
        end_dt = datetime.fromisoformat(self.ended_at)
        s_utc = start_dt.replace(tzinfo=UTC) if start_dt.tzinfo is None else start_dt
        e_utc = end_dt.replace(tzinfo=UTC) if end_dt.tzinfo is None else end_dt
        secs = max(ZERO_AMOUNT, (e_utc - s_utc).total_seconds())
        return round(secs / SECONDS_PER_HOUR, DECIMAL_PLACES)

    def to_dict(self) -> dict[str, object]:
        """Serialize travel trip attributes to a dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "destination": self.destination,
            "country_code": self.country_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "transport_mode": self.transport_mode,
            "distance_km": self.distance_km,
            "meals_by_date": {k: v.to_dict() for k, v in self.meals_by_date.items()},
            "entity_id": self.entity_id,
            "notes": self.notes,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class TravelDayAllowance:
    """Calculated statutory per-diem and meal deductions for a single travel calendar day."""

    day: str
    classification: str
    gross_per_diem: float
    meal_deduction: float
    net_per_diem: float
    breakfast_provided: bool
    lunch_provided: bool
    dinner_provided: bool

    def to_dict(self) -> dict[str, object]:
        """Serialize daily allowance results to a dictionary."""
        return {
            "day": self.day,
            "classification": self.classification,
            "gross_per_diem": self.gross_per_diem,
            "meal_deduction": self.meal_deduction,
            "net_per_diem": self.net_per_diem,
            "breakfast_provided": self.breakfast_provided,
            "lunch_provided": self.lunch_provided,
            "dinner_provided": self.dinner_provided,
        }


@dataclass(frozen=True, slots=True)
class TravelTripCalculation:
    """Statutory reimbursement and tax allowance evaluation for a complete travel trip."""

    trip: TravelTrip
    days: tuple[TravelDayAllowance, ...]
    gross_per_diem_total: float
    meal_deduction_total: float
    net_per_diem_total: float
    mileage_allowance: float
    total_reimbursement: float
    currency: str

    def to_dict(self) -> dict[str, object]:
        """Serialize trip calculation breakdown to a dictionary."""
        return {
            "trip": self.trip.to_dict(),
            "days": [d.to_dict() for d in self.days],
            "gross_per_diem_total": self.gross_per_diem_total,
            "meal_deduction_total": self.meal_deduction_total,
            "net_per_diem_total": self.net_per_diem_total,
            "mileage_allowance": self.mileage_allowance,
            "total_reimbursement": self.total_reimbursement,
            "currency": self.currency,
        }


# endregion


# region Calculation
def _compute_day_allowance(
    day_str: str,
    classification: str,
    gross: float,
    meals: DayMealFlags | None,
    pack: ReportingPack,
    full_rate: float,
) -> TravelDayAllowance:
    """Compute meal deductions and net per-diem for one travel day."""
    breakfast = meals.breakfast if meals else False
    lunch = meals.lunch if meals else False
    dinner = meals.dinner if meals else False

    if gross > ZERO_AMOUNT and pack.tax_rules_enabled:
        deduction = ZERO_AMOUNT
        if breakfast:
            deduction += round(pack.breakfast_deduction_ratio * full_rate, DECIMAL_PLACES)
        if lunch:
            deduction += round(pack.lunch_deduction_ratio * full_rate, DECIMAL_PLACES)
        if dinner:
            deduction += round(pack.dinner_deduction_ratio * full_rate, DECIMAL_PLACES)
        meal_deduction = round(deduction, DECIMAL_PLACES)
        net_per_diem = max(ZERO_AMOUNT, round(gross - meal_deduction, DECIMAL_PLACES))
    else:
        meal_deduction = ZERO_AMOUNT
        net_per_diem = ZERO_AMOUNT

    return TravelDayAllowance(
        day=day_str,
        classification=classification,
        gross_per_diem=round(gross, DECIMAL_PLACES),
        meal_deduction=meal_deduction,
        net_per_diem=net_per_diem,
        breakfast_provided=breakfast,
        lunch_provided=lunch,
        dinner_provided=dinner,
    )


def _calculate_days_allowances(
    trip: TravelTrip,
    pack: ReportingPack,
    partial_rate: float,
    full_rate: float,
) -> tuple[TravelDayAllowance, ...]:
    """Compute itemized daily allowances and statutory meal deductions."""
    touched = trip.touched_dates()
    if len(touched) == 1:
        is_over = trip.duration_hours() > pack.per_diem_min_hours_single_day
        cls_name = DAY_CLASS_SINGLE_OVER_8H if is_over else DAY_CLASS_SINGLE_UNDER_8H
        gross = partial_rate if is_over else ZERO_AMOUNT
        day_calc = _compute_day_allowance(
            touched[0],
            cls_name,
            gross,
            trip.meals_by_date.get(touched[0]),
            pack,
            full_rate,
        )
        return (day_calc,)
    if len(touched) >= 2:
        days: list[TravelDayAllowance] = []
        for idx, day_str in enumerate(touched):
            is_endpoint = idx == 0 or idx == len(touched) - 1
            cls_name = DAY_CLASS_ARRIVAL_OR_DEPARTURE if is_endpoint else DAY_CLASS_FULL_24H
            gross = partial_rate if is_endpoint else full_rate
            days.append(
                _compute_day_allowance(
                    day_str,
                    cls_name,
                    gross,
                    trip.meals_by_date.get(day_str),
                    pack,
                    full_rate,
                )
            )
        return tuple(days)
    return ()


def _calculate_mileage_allowance(trip: TravelTrip, pack: ReportingPack) -> float:
    """Calculate private vehicle mileage reimbursement for business travel."""
    if (
        pack.tax_rules_enabled
        and trip.transport_mode == TRANSPORT_MODE_CAR
        and trip.distance_km > ZERO_AMOUNT
    ):
        return round(trip.distance_km * pack.car_mileage_rate_per_km, DECIMAL_PLACES)
    return ZERO_AMOUNT


def calculate_trip_allowance(
    trip: TravelTrip,
    pack: ReportingPack,
) -> TravelTripCalculation:
    """Compute statutory daily per-diem allowances, meal deductions, and mileage."""
    partial_rate, full_rate = pack.per_diem_rates_for_country(trip.country_code)
    if not pack.tax_rules_enabled:
        partial_rate, full_rate = ZERO_AMOUNT, ZERO_AMOUNT

    days = _calculate_days_allowances(trip, pack, partial_rate, full_rate)
    mileage = _calculate_mileage_allowance(trip, pack)
    gross_total = round(sum(d.gross_per_diem for d in days), DECIMAL_PLACES)
    deduction_total = round(sum(d.meal_deduction for d in days), DECIMAL_PLACES)
    net_total = round(sum(d.net_per_diem for d in days), DECIMAL_PLACES)

    return TravelTripCalculation(
        trip=trip,
        days=days,
        gross_per_diem_total=gross_total,
        meal_deduction_total=deduction_total,
        net_per_diem_total=net_total,
        mileage_allowance=mileage,
        total_reimbursement=round(net_total + mileage, DECIMAL_PLACES),
        currency=pack.currency,
    )


# endregion


# region Lazy Import
def __getattr__(name: str) -> object:
    """Lazy-load TravelTripStore from storage module to prevent circular imports."""
    if name == "TravelTripStore":
        from worktrack.reporting.store import TravelTripStore

        return TravelTripStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# endregion

__all__ = [
    "ALLOWED_TRANSPORT_MODES",
    "DAY_CLASS_ARRIVAL_OR_DEPARTURE",
    "DAY_CLASS_FULL_24H",
    "DAY_CLASS_SINGLE_OVER_8H",
    "DAY_CLASS_SINGLE_UNDER_8H",
    "PER_DIEM_MIN_SINGLE_DAY_HOURS",
    "TRANSPORT_MODE_CAR",
    "TRANSPORT_MODE_FLIGHT",
    "TRANSPORT_MODE_OTHER",
    "TRANSPORT_MODE_PUBLIC_TRANSIT",
    "TRANSPORT_MODE_TRAIN",
    "DayMealFlags",
    "TravelDayAllowance",
    "TravelTrip",
    "TravelTripCalculation",
    "TravelTripStore",
    "calculate_trip_allowance",
]
