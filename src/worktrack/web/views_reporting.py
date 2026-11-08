"""DRF APIView endpoints for Wave 5 statutory reporting, holidays, absences, and travel."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from worktrack.absences import HolidayCalendar
from worktrack.reporting import (
    DayMealFlags,
    JurisdictionReportSummary,
    ReportingEngine,
    ReportingPack,
    ReportingPackRegistry,
    calculate_trip_allowance,
    export_report_csv,
    export_report_json,
    export_report_markdown,
    load_custom_holidays_from_toml,
)
from worktrack.web.serializers_reporting import (
    AbsenceCreateRequestSerializer,
    AbsenceEntrySerializer,
    AbsenceListResponseSerializer,
    HolidayListResponseSerializer,
    JurisdictionReportSummarySerializer,
    ReportingPackListResponseSerializer,
    TravelTripCalculationSerializer,
    TravelTripCreateRequestSerializer,
    TravelTripListResponseSerializer,
    VacationQuotaRequestSerializer,
    VacationQuotaSerializer,
    VacationSummarySerializer,
)
from worktrack.web.services import WebServiceContainer, get_active_web_services

# region Constants
DEFAULT_COUNTRY_CODE: Final = "DE"
DATE_YEAR_SLICE_LEN: Final = 4
REPORT_FORMAT_JSON: Final = "json"
REPORT_FORMAT_CSV: Final = "csv"
REPORT_FORMAT_MARKDOWN: Final = "markdown"
# endregion


# region Helpers
def _get_holiday_calendar(
    services: WebServiceContainer, explicit_subdiv: str | None
) -> HolidayCalendar:
    """Build a `HolidayCalendar` for the requested subdivision or container default."""
    subdiv = (
        explicit_subdiv.strip()
        if explicit_subdiv
        else (services.settings.holiday_subdivision or DEFAULT_COUNTRY_CODE)
    )
    custom_holidays = load_custom_holidays_from_toml(services.settings.config_file)
    return HolidayCalendar(subdivision=subdiv, custom_holidays=custom_holidays)


def _build_meal_flags(
    breakfast_dates: list[str],
    lunch_dates: list[str],
    dinner_dates: list[str],
) -> dict[str, DayMealFlags]:
    """Construct a date-to-`DayMealFlags` dictionary from provided meal date lists."""
    b_set = set(breakfast_dates)
    l_set = set(lunch_dates)
    d_set = set(dinner_dates)
    all_dates = sorted(b_set | l_set | d_set)
    return {
        d: DayMealFlags(breakfast=d in b_set, lunch=d in l_set, dinner=d in d_set)
        for d in all_dates
    }


# endregion


# region Pack Views
class ReportingPackListView(APIView):  # type: ignore[misc]
    """List all registered statutory and user-configured reporting packs."""

    @extend_schema(
        operation_id="v1_reporting_packs_list",
        responses={200: ReportingPackListResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        """Return all available reporting packs."""
        services = get_active_web_services()
        registry = ReportingPackRegistry.from_toml_file(services.settings.config_file)
        packs = [p.to_dict() for p in registry.list_packs()]
        return Response({"packs": packs}, status=status.HTTP_200_OK)


# endregion


# region Holiday & Absence Views
class ReportingHolidaysView(APIView):  # type: ignore[misc]
    """Retrieve statutory public holidays for a calendar year and regional subdivision."""

    @extend_schema(
        operation_id="v1_reporting_holidays_list",
        parameters=[
            OpenApiParameter("year", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
            OpenApiParameter(
                "subdivision", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False
            ),
        ],
        responses={200: HolidayListResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        """Return statutory and custom public holidays."""
        services = get_active_web_services()
        year_raw = request.query_params.get("year")
        year = int(year_raw) if year_raw else datetime.now(UTC).year
        subdiv = request.query_params.get("subdivision")
        cal = _get_holiday_calendar(services, subdiv)
        holidays = [h.to_dict() for h in cal.holidays_for_year(year)]
        return Response({"holidays": holidays, "count": len(holidays)}, status=status.HTTP_200_OK)


class ReportingAbsenceListCreateView(APIView):  # type: ignore[misc]
    """List or record statutory leaves, vacation days, and compensatory time."""

    @extend_schema(
        operation_id="v1_reporting_absences_list",
        parameters=[
            OpenApiParameter(
                "start_date", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False
            ),
            OpenApiParameter("end_date", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("kind", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter(
                "subdivision", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False
            ),
        ],
        responses={200: AbsenceListResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        """List absences matching optional date bounds and category filters."""
        services = get_active_web_services()
        cal = _get_holiday_calendar(services, request.query_params.get("subdivision"))
        with services.open_absence_ledger(calendar=cal) as ledger:
            items = ledger.list_absences(
                start_date=request.query_params.get("start_date"),
                end_date=request.query_params.get("end_date"),
                kind=request.query_params.get("kind"),
            )
            serialized = [e.to_dict(cal) for e in items]
        return Response(
            {"absences": serialized, "count": len(serialized)}, status=status.HTTP_200_OK
        )

    @extend_schema(
        operation_id="v1_reporting_absences_create",
        request=AbsenceCreateRequestSerializer,
        responses={201: AbsenceEntrySerializer},
    )
    def post(self, request: Request) -> Response:
        """Record a new absence entry in the ledger."""
        serializer = AbsenceCreateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services = get_active_web_services()
        cal = _get_holiday_calendar(services, data.get("subdivision"))
        with services.open_absence_ledger(calendar=cal) as ledger:
            entry = ledger.record_absence(
                start_date=str(data["start_date"]),
                end_date=data.get("end_date"),
                kind=str(data.get("kind", "vacation")),
                day_fraction=float(data.get("day_fraction", 1.0)),
                notes=str(data.get("notes", "")),
            )
            payload = entry.to_dict(cal)
        return Response(payload, status=status.HTTP_201_CREATED)


class ReportingAbsenceQuotaView(APIView):  # type: ignore[misc]
    """Manage annual vacation entitlements and carried-over leave balances."""

    @extend_schema(
        operation_id="v1_reporting_absence_quota_retrieve",
        parameters=[
            OpenApiParameter("year", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: VacationQuotaSerializer},
    )
    def get(self, request: Request) -> Response:
        """Retrieve stored vacation quota for a calendar year."""
        services = get_active_web_services()
        year_raw = request.query_params.get("year")
        year = int(year_raw) if year_raw else datetime.now(UTC).year
        with services.open_absence_ledger() as ledger:
            quota = ledger.get_quota(year)
        return Response(quota.to_dict(), status=status.HTTP_200_OK)

    @extend_schema(
        operation_id="v1_reporting_absence_quota_update",
        parameters=[
            OpenApiParameter("year", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
        ],
        request=VacationQuotaRequestSerializer,
        responses={200: VacationQuotaSerializer},
    )
    def put(self, request: Request) -> Response:
        """Create or update statutory vacation quota for a year."""
        serializer = VacationQuotaRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        year_raw = request.query_params.get("year") or data.get("year")
        year = int(year_raw) if year_raw else datetime.now(UTC).year
        services = get_active_web_services()
        with services.open_absence_ledger() as ledger:
            quota = ledger.set_quota(
                year=year,
                entitlement_days=float(data["entitlement_days"]),
                carryover_days=float(data.get("carryover_days", 0.0)),
            )
        return Response(quota.to_dict(), status=status.HTTP_200_OK)


class ReportingAbsenceSummaryView(APIView):  # type: ignore[misc]
    """Summarize annual leave balances, taken vacation, and holiday metrics."""

    @extend_schema(
        operation_id="v1_reporting_absence_summary_retrieve",
        parameters=[
            OpenApiParameter("year", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
            OpenApiParameter(
                "subdivision", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False
            ),
            OpenApiParameter(
                "reference_date", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False
            ),
        ],
        responses={200: VacationSummarySerializer},
    )
    def get(self, request: Request) -> Response:
        """Compute consolidated annual leave breakdown."""
        services = get_active_web_services()
        year_raw = request.query_params.get("year")
        year = int(year_raw) if year_raw else datetime.now(UTC).year
        cal = _get_holiday_calendar(services, request.query_params.get("subdivision"))
        ref_date = request.query_params.get("reference_date")
        with services.open_absence_ledger(calendar=cal) as ledger:
            summary = ledger.summarize_year(year, reference_date=ref_date)
        return Response(summary.to_dict(), status=status.HTTP_200_OK)


# endregion


# region Travel Views
class ReportingTravelTripListCreateView(APIView):  # type: ignore[misc]
    """Record and list business travel trips with itemized per-diem calculations."""

    @extend_schema(
        operation_id="v1_reporting_travel_trips_list",
        parameters=[
            OpenApiParameter(
                "start_date", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False
            ),
            OpenApiParameter("end_date", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("pack", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: TravelTripListResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        """List business travel trips and their calculated statutory allowances."""
        services = get_active_web_services()
        registry = ReportingPackRegistry.from_toml_file(services.settings.config_file)
        pack_slug = request.query_params.get("pack") or services.settings.reporting_pack
        pack = registry.get_pack(pack_slug)
        with services.open_travel_store() as store:
            trips = store.list_trips(
                start_date=request.query_params.get("start_date"),
                end_date=request.query_params.get("end_date"),
            )
            calcs = [calculate_trip_allowance(t, pack).to_dict() for t in trips]
        return Response({"trips": calcs, "count": len(calcs)}, status=status.HTTP_200_OK)

    @extend_schema(
        operation_id="v1_reporting_travel_trips_create",
        request=TravelTripCreateRequestSerializer,
        responses={201: TravelTripCalculationSerializer},
    )
    def post(self, request: Request) -> Response:
        """Record a business travel trip and calculate per-diem and mileage allowances."""
        serializer = TravelTripCreateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services = get_active_web_services()
        registry = ReportingPackRegistry.from_toml_file(services.settings.config_file)
        pack_slug = data.get("pack") or services.settings.reporting_pack
        pack = registry.get_pack(str(pack_slug))

        meals = _build_meal_flags(
            data.get("breakfast_dates", []),
            data.get("lunch_dates", []),
            data.get("dinner_dates", []),
        )
        with services.open_travel_store() as store:
            trip = store.record_trip(
                title=str(data["title"]),
                destination=str(data["destination"]),
                started_at=str(data["started_at"]),
                ended_at=str(data["ended_at"]),
                country_code=str(data.get("country_code", DEFAULT_COUNTRY_CODE)),
                transport_mode=str(data.get("transport_mode", "train")),
                distance_km=float(data.get("distance_km", 0.0)),
                meals_by_date=meals,
                entity_id=data.get("entity_id"),
                notes=str(data.get("notes", "")),
            )
            calc = calculate_trip_allowance(trip, pack)
        return Response(calc.to_dict(), status=status.HTTP_201_CREATED)


# endregion


# region Summary Views
def _render_report_summary_text(
    summary: JurisdictionReportSummary, report_format: str
) -> str:
    """Render summary into formatted text based on the selected report format."""
    if report_format == REPORT_FORMAT_CSV:
        return str(export_report_csv(summary))
    if report_format == REPORT_FORMAT_MARKDOWN:
        return str(export_report_markdown(summary))
    return str(export_report_json(summary))


def _compute_jurisdiction_summary(
    services: WebServiceContainer,
    *,
    start_date: str,
    end_date: str,
    pack: ReportingPack,
    cal: HolidayCalendar,
    one_way_km: float,
) -> JurisdictionReportSummary:
    """Execute reporting engine with absence exclusions and business travel store."""
    with (
        services.open_absence_ledger(calendar=cal) as abs_ledger,
        services.open_travel_store() as travel_store,
        services.open_ledger() as ledger,
    ):
        excluded = abs_ledger.excluded_workplace_dates(
            start_date, end_date, include_public_holidays=True
        )
        year = int(start_date[:DATE_YEAR_SLICE_LEN])
        abs_summary: Mapping[str, object] = abs_ledger.summarize_year(year).to_dict()
        return ReportingEngine().build_summary(
            ledger=ledger,
            pack=pack,
            start_date=start_date,
            end_date=end_date,
            office_one_way_km=one_way_km,
            excluded_dates=excluded,
            travel_store=travel_store,
            absence_summary=abs_summary,
        )


class ReportingSummaryView(APIView):  # type: ignore[misc]
    """Synthesize statutory workplace facts, absences, and business travel into a report."""

    @extend_schema(
        operation_id="v1_reporting_summary_retrieve",
        parameters=[
            OpenApiParameter("start_date", OpenApiTypes.STR, OpenApiParameter.QUERY, required=True),
            OpenApiParameter("end_date", OpenApiTypes.STR, OpenApiParameter.QUERY, required=True),
            OpenApiParameter("pack", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
            OpenApiParameter(
                "subdivision", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False
            ),
            OpenApiParameter(
                "office_one_way_km", OpenApiTypes.FLOAT, OpenApiParameter.QUERY, required=False
            ),
            OpenApiParameter(
                "report_format",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=False,
                enum=[REPORT_FORMAT_JSON, REPORT_FORMAT_CSV, REPORT_FORMAT_MARKDOWN],
            ),
        ],
        responses={200: JurisdictionReportSummarySerializer},
    )
    def get(self, request: Request) -> Response:
        """Calculate statutory tax summary with deductions and exclusions."""
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        if not start_date or not end_date:
            return Response(
                {
                    "detail": "start_date and end_date are required query parameters.",
                    "code": "validation_error",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        services = get_active_web_services()
        registry = ReportingPackRegistry.from_toml_file(services.settings.config_file)
        pack_slug = request.query_params.get("pack") or services.settings.reporting_pack
        pack = registry.get_pack(pack_slug)
        cal = _get_holiday_calendar(services, request.query_params.get("subdivision"))
        km_raw = request.query_params.get("office_one_way_km")
        one_way_km = float(km_raw) if km_raw is not None else services.settings.office_one_way_km
        report_format = request.query_params.get("report_format", REPORT_FORMAT_JSON).lower()

        summary = _compute_jurisdiction_summary(
            services,
            start_date=start_date,
            end_date=end_date,
            pack=pack,
            cal=cal,
            one_way_km=one_way_km,
        )
        rendered = _render_report_summary_text(summary, report_format)
        payload = {**summary.to_dict(), "rendered": rendered, "report_format": report_format}
        return Response(payload, status=status.HTTP_200_OK)


# endregion
