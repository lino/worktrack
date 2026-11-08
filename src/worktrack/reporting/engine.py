"""ReportingEngine synthesizing workplace facts, statutory packs, and travel logs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from worktrack.core import Ledger
from worktrack.reporting.exporters import (
    export_report_csv,
    export_report_json,
    export_report_markdown,
    reporting_summary_csvw_schema,
    reporting_summary_json_schema,
)
from worktrack.reporting.packs import ReportingPack
from worktrack.reporting.store import TravelTripStore
from worktrack.reporting.travel import (
    TravelTripCalculation,
    calculate_trip_allowance,
)

# region Constants
FACET_KEY_WORKPLACE: Final = "workplace"
WORKPLACE_VAL_HOME_OFFICE: Final = "home_office"
WORKPLACE_VAL_OFFICE: Final = "office"
WORKPLACE_VAL_MOBILE_WORK: Final = "mobile_work"
WORKPLACE_VAL_MOBILE: Final = "mobile"
WORKPLACE_VAL_CLIENT_SITE: Final = "client_site"
WORKPLACE_VAL_CLIENT: Final = "client"

DECIMAL_PRECISION: Final = 2
ZERO_AMOUNT: Final = 0.0
ZERO_DAYS: Final = 0
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class JurisdictionReportSummary:
    """Comprehensive statutory summary for a jurisdiction across a calendar window.

    Attributes:
        pack: Statutory or generic ReportingPack applied.
        start_date: Inclusive window start date `YYYY-MM-DD`.
        end_date: Inclusive window end date `YYYY-MM-DD`.
        office_one_way_km: Commute distance from home to office in km.
        home_office_days_raw: Total calendar days tagged as home office.
        home_office_days_eligible: Days eligible after absence/commute/travel deduplication.
        home_office_days_capped: Days capped by jurisdiction statutory maximum.
        home_office_allowance: Monetary allowance for home office.
        office_days_raw: Total calendar days tagged as office.
        office_days_eligible: Office days eligible after absence exclusions.
        commute_daily_allowance: Tiered daily commute allowance for one office day.
        commute_allowance_total: Monetary allowance for office commutes.
        mobile_work_days: Total days worked in mobile transit.
        client_site_days: Total days worked at external client sites.
        excluded_absence_or_holiday_dates: Absence, vacation, or public holiday dates.
        excluded_travel_dates: Full 24h or single-day >8h business travel dates.
        trips: Itemized business travel evaluations.
        travel_per_diem_total: Net daily per-diem allowance across all trips.
        travel_mileage_total: Private vehicle mileage reimbursements.
        travel_allowance_total: Total business travel reimbursements.
        absence_summary: Optional summary mapping from absences engine.
        total_allowance_and_reimbursement: Grand total statutory tax and travel claim.
    """

    pack: ReportingPack
    start_date: str
    end_date: str
    office_one_way_km: float
    home_office_days_raw: int
    home_office_days_eligible: int
    home_office_days_capped: int
    home_office_allowance: float
    office_days_raw: int
    office_days_eligible: int
    commute_daily_allowance: float
    commute_allowance_total: float
    mobile_work_days: int
    client_site_days: int
    excluded_absence_or_holiday_dates: tuple[str, ...]
    excluded_travel_dates: tuple[str, ...]
    trips: tuple[TravelTripCalculation, ...]
    travel_per_diem_total: float
    travel_mileage_total: float
    travel_allowance_total: float
    absence_summary: Mapping[str, object] | None
    total_allowance_and_reimbursement: float

    def to_dict(self) -> dict[str, object]:
        """Serialize jurisdiction report summary to a structured dictionary."""
        return {
            "pack": self.pack.to_dict(),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "office_one_way_km": self.office_one_way_km,
            "home_office_days_raw": self.home_office_days_raw,
            "home_office_days_eligible": self.home_office_days_eligible,
            "home_office_days_capped": self.home_office_days_capped,
            "home_office_allowance": self.home_office_allowance,
            "office_days_raw": self.office_days_raw,
            "office_days_eligible": self.office_days_eligible,
            "commute_daily_allowance": self.commute_daily_allowance,
            "commute_allowance_total": self.commute_allowance_total,
            "mobile_work_days": self.mobile_work_days,
            "client_site_days": self.client_site_days,
            "excluded_absence_or_holiday_dates": list(self.excluded_absence_or_holiday_dates),
            "excluded_travel_dates": list(self.excluded_travel_dates),
            "trips": [t.to_dict() for t in self.trips],
            "travel_per_diem_total": self.travel_per_diem_total,
            "travel_mileage_total": self.travel_mileage_total,
            "travel_allowance_total": self.travel_allowance_total,
            "absence_summary": (
                dict(self.absence_summary) if self.absence_summary is not None else None
            ),
            "total_allowance_and_reimbursement": self.total_allowance_and_reimbursement,
        }


# endregion


# region Engine
class ReportingEngine:
    """Reporting engine calculating tax allowances and business travel reimbursements."""

    def build_summary(
        self,
        ledger: Ledger,
        *,
        pack: ReportingPack,
        start_date: str,
        end_date: str,
        office_one_way_km: float = ZERO_AMOUNT,
        excluded_dates: set[str] | None = None,
        travel_store: TravelTripStore | None = None,
        absence_summary: Mapping[str, object] | None = None,
    ) -> JurisdictionReportSummary:
        """Synthesize ledger workplace facts and travel trips into statutory summary.

        Args:
            ledger: Active Worktrack ledger containing workplace context facts.
            pack: Statutory reporting pack to evaluate.
            start_date: Inclusive start date `YYYY-MM-DD`.
            end_date: Inclusive end date `YYYY-MM-DD`.
            office_one_way_km: One-way commute distance in kilometers.
            excluded_dates: Optional set of vacation or holiday dates to exclude.
            travel_store: Optional TravelTripStore with recorded business trips.
            absence_summary: Optional absences summary dictionary.

        Returns:
            Computed JurisdictionReportSummary.
        """
        ho_dates, off_dates, mob_dates, cli_dates = _collect_workplace_dates(
            ledger, start_date, end_date
        )
        norm_excluded = set(excluded_dates) if excluded_dates else set()
        trips, trav_dates, per_diem_tot, mileage_tot, trav_tot = _process_travel(
            pack, travel_store, start_date, end_date
        )

        wp_calcs = _compute_workplace_allowances(
            pack=pack,
            ho_dates=ho_dates,
            off_dates=off_dates,
            excluded_dates=norm_excluded,
            travel_dates=trav_dates,
            office_one_way_km=office_one_way_km,
        )

        grand_total = round(
            wp_calcs["ho_allowance"] + wp_calcs["commute_tot"] + trav_tot,
            DECIMAL_PRECISION,
        )

        return JurisdictionReportSummary(
            pack=pack,
            start_date=start_date,
            end_date=end_date,
            office_one_way_km=max(ZERO_AMOUNT, office_one_way_km),
            home_office_days_raw=len(ho_dates),
            home_office_days_eligible=int(wp_calcs["ho_eligible_count"]),
            home_office_days_capped=int(wp_calcs["ho_capped_count"]),
            home_office_allowance=float(wp_calcs["ho_allowance"]),
            office_days_raw=len(off_dates),
            office_days_eligible=int(wp_calcs["off_eligible_count"]),
            commute_daily_allowance=float(wp_calcs["daily_commute"]),
            commute_allowance_total=float(wp_calcs["commute_tot"]),
            mobile_work_days=len(mob_dates),
            client_site_days=len(cli_dates),
            excluded_absence_or_holiday_dates=tuple(sorted(norm_excluded)),
            excluded_travel_dates=tuple(sorted(trav_dates)),
            trips=trips,
            travel_per_diem_total=per_diem_tot,
            travel_mileage_total=mileage_tot,
            travel_allowance_total=trav_tot,
            absence_summary=absence_summary,
            total_allowance_and_reimbursement=grand_total,
        )


def _collect_workplace_dates(
    ledger: Ledger, start_date: str, end_date: str
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Index distinct dates for home office, office, mobile, and client workplaces."""
    ho_dates: set[str] = set()
    off_dates: set[str] = set()
    mob_dates: set[str] = set()
    cli_dates: set[str] = set()

    facts = ledger.list_context_facts(
        facet_key=FACET_KEY_WORKPLACE, since_date=start_date, until_date=end_date
    )
    for fact in facts:
        scope = fact.scope_date
        if not scope or not (start_date <= scope <= end_date):
            continue
        val = str(fact.value).strip().lower()
        if val == WORKPLACE_VAL_HOME_OFFICE:
            ho_dates.add(scope)
        elif val == WORKPLACE_VAL_OFFICE:
            off_dates.add(scope)
        elif val in (WORKPLACE_VAL_MOBILE_WORK, WORKPLACE_VAL_MOBILE):
            mob_dates.add(scope)
        elif val in (WORKPLACE_VAL_CLIENT_SITE, WORKPLACE_VAL_CLIENT):
            cli_dates.add(scope)

    return ho_dates, off_dates, mob_dates, cli_dates


def _compute_workplace_allowances(
    *,
    pack: ReportingPack,
    ho_dates: set[str],
    off_dates: set[str],
    excluded_dates: set[str],
    travel_dates: set[str],
    office_one_way_km: float,
) -> dict[str, float | int]:
    """Calculate eligible counts, caps, and monetary allowances for workplace."""
    off_eligible = off_dates - excluded_dates
    ho_eligible = ho_dates - excluded_dates - off_dates - travel_dates

    if pack.tax_rules_enabled:
        if pack.home_office_max_days > 0:
            ho_capped = min(len(ho_eligible), pack.home_office_max_days)
        else:
            ho_capped = len(ho_eligible)
        ho_allowance = round(ho_capped * pack.home_office_daily_rate, DECIMAL_PRECISION)
        daily_commute = pack.daily_commute_allowance(office_one_way_km)
        commute_tot = round(len(off_eligible) * daily_commute, DECIMAL_PRECISION)
    else:
        ho_capped = ZERO_DAYS
        ho_allowance = ZERO_AMOUNT
        daily_commute = ZERO_AMOUNT
        commute_tot = ZERO_AMOUNT

    return {
        "ho_eligible_count": len(ho_eligible),
        "ho_capped_count": ho_capped,
        "ho_allowance": ho_allowance,
        "off_eligible_count": len(off_eligible),
        "daily_commute": daily_commute,
        "commute_tot": commute_tot,
    }


def _process_travel(
    pack: ReportingPack,
    travel_store: TravelTripStore | None,
    start_date: str,
    end_date: str,
) -> tuple[tuple[TravelTripCalculation, ...], set[str], float, float, float]:
    """Calculate per-diem and mileage totals and gather travel dates for exclusion."""
    if travel_store is None:
        return (), set(), ZERO_AMOUNT, ZERO_AMOUNT, ZERO_AMOUNT

    trips_raw = travel_store.list_trips(start_date=start_date, end_date=end_date)
    calcs = tuple(calculate_trip_allowance(t, pack) for t in trips_raw)
    travel_dates = travel_store.travel_dates_in_range(start_date, end_date)

    if pack.tax_rules_enabled:
        per_diem = round(sum(c.net_per_diem_total for c in calcs), DECIMAL_PRECISION)
        mileage = round(sum(c.mileage_allowance for c in calcs), DECIMAL_PRECISION)
        total = round(per_diem + mileage, DECIMAL_PRECISION)
    else:
        per_diem = ZERO_AMOUNT
        mileage = ZERO_AMOUNT
        total = ZERO_AMOUNT

    return calcs, travel_dates, per_diem, mileage, total


# endregion

__all__ = [
    "JurisdictionReportSummary",
    "ReportingEngine",
    "export_report_csv",
    "export_report_json",
    "export_report_markdown",
    "reporting_summary_csvw_schema",
    "reporting_summary_json_schema",
]
