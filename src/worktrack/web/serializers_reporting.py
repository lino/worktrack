"""DRF serializers for Wave 5 statutory reporting, holiday, absence, and travel endpoints."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from worktrack.absences.models import ABSENCE_KIND_VACATION, ABSENCE_KINDS, ALLOWED_DAY_FRACTIONS
from worktrack.reporting.travel import ALLOWED_TRANSPORT_MODES


# region Pack Serializers
@extend_schema_serializer(component_name="ReportingPack")
class ReportingPackSerializer(serializers.Serializer):  # type: ignore[misc]
    """Statutory or generic reporting pack configuration."""

    slug = serializers.CharField()
    title = serializers.CharField()
    currency = serializers.CharField()
    country_code = serializers.CharField()
    tax_rules_enabled = serializers.BooleanField()
    home_office_daily_rate = serializers.FloatField()
    home_office_max_days = serializers.IntegerField()
    commute_first_tier_km = serializers.FloatField()
    commute_first_tier_rate = serializers.FloatField()
    commute_second_tier_rate = serializers.FloatField()
    per_diem_partial_day_rate = serializers.FloatField()
    per_diem_full_day_rate = serializers.FloatField()
    per_diem_min_hours_single_day = serializers.FloatField()
    breakfast_deduction_ratio = serializers.FloatField()
    lunch_deduction_ratio = serializers.FloatField()
    dinner_deduction_ratio = serializers.FloatField()
    car_mileage_rate_per_km = serializers.FloatField()
    country_per_diem_overrides = serializers.DictField(
        child=serializers.ListField(child=serializers.FloatField()),
        required=False,
    )


@extend_schema_serializer(component_name="ReportingPackListResponse")
class ReportingPackListResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `GET /api/v1/reporting/packs/`."""

    packs = ReportingPackSerializer(many=True)


# endregion


# region Holiday Serializers
@extend_schema_serializer(component_name="PublicHoliday")
class PublicHolidaySerializer(serializers.Serializer):  # type: ignore[misc]
    """Statutory or custom public holiday descriptor."""

    date = serializers.CharField()
    name = serializers.CharField()
    subdivision = serializers.CharField()
    is_custom = serializers.BooleanField()


@extend_schema_serializer(component_name="HolidayListResponse")
class HolidayListResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `GET /api/v1/reporting/holidays/`."""

    holidays = PublicHolidaySerializer(many=True)
    count = serializers.IntegerField()


# endregion


# region Absence Serializers
class AbsenceCreateRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request payload for recording a new statutory absence."""

    start_date = serializers.CharField(required=True)
    end_date = serializers.CharField(required=False, allow_null=True)
    kind = serializers.ChoiceField(
        choices=sorted(ABSENCE_KINDS), default=ABSENCE_KIND_VACATION, required=False
    )
    day_fraction = serializers.ChoiceField(
        choices=sorted(ALLOWED_DAY_FRACTIONS), default=1.0, required=False
    )
    notes = serializers.CharField(default="", required=False, allow_blank=True)
    subdivision = serializers.CharField(default="", required=False, allow_blank=True)


@extend_schema_serializer(component_name="AbsenceEntry")
class AbsenceEntrySerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical absence record representation."""

    id = serializers.CharField()
    start_date = serializers.CharField()
    end_date = serializers.CharField()
    kind = serializers.CharField()
    day_fraction = serializers.FloatField()
    notes = serializers.CharField()
    created_at = serializers.CharField()
    workdays_covered = serializers.ListField(child=serializers.CharField())
    effective_days = serializers.FloatField()


@extend_schema_serializer(component_name="AbsenceListResponse")
class AbsenceListResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `GET /api/v1/reporting/absences/`."""

    absences = AbsenceEntrySerializer(many=True)
    count = serializers.IntegerField()


class VacationQuotaRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request payload for updating an annual vacation quota."""

    year = serializers.IntegerField(required=False)
    entitlement_days = serializers.FloatField(required=True)
    carryover_days = serializers.FloatField(default=0.0, required=False)


@extend_schema_serializer(component_name="VacationQuota")
class VacationQuotaSerializer(serializers.Serializer):  # type: ignore[misc]
    """Annual vacation quota and carryover balance."""

    year = serializers.IntegerField()
    entitlement_days = serializers.FloatField()
    carryover_days = serializers.FloatField()
    total_available_days = serializers.FloatField()
    updated_at = serializers.CharField()


@extend_schema_serializer(component_name="VacationSummary")
class VacationSummarySerializer(serializers.Serializer):  # type: ignore[misc]
    """Consolidated annual leave breakdown and quota balance."""

    year = serializers.IntegerField()
    subdivision = serializers.CharField()
    entitlement_days = serializers.FloatField()
    carryover_days = serializers.FloatField()
    total_quota_days = serializers.FloatField()
    taken_vacation_days = serializers.FloatField()
    planned_vacation_days = serializers.FloatField()
    remaining_vacation_days = serializers.FloatField()
    sick_leave_days = serializers.FloatField()
    compensatory_off_days = serializers.FloatField()
    special_leave_days = serializers.FloatField()
    unpaid_leave_days = serializers.FloatField()
    public_holiday_count = serializers.IntegerField()
    weekday_public_holiday_count = serializers.IntegerField()


# endregion


# region Travel Serializers
class TravelTripCreateRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request payload for recording a business travel trip."""

    title = serializers.CharField(required=True)
    destination = serializers.CharField(required=True)
    started_at = serializers.CharField(required=True)
    ended_at = serializers.CharField(required=True)
    country_code = serializers.CharField(default="DE", required=False)
    transport_mode = serializers.ChoiceField(
        choices=sorted(ALLOWED_TRANSPORT_MODES), default="train", required=False
    )
    distance_km = serializers.FloatField(default=0.0, required=False)
    breakfast_dates = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    lunch_dates = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    dinner_dates = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    entity_id = serializers.CharField(required=False, allow_null=True)
    notes = serializers.CharField(default="", required=False, allow_blank=True)
    pack = serializers.CharField(required=False, allow_null=True)


@extend_schema_serializer(component_name="TravelDayAllowance")
class TravelDayAllowanceSerializer(serializers.Serializer):  # type: ignore[misc]
    """Calculated per-diem and meal deductions for a single travel calendar day."""

    day = serializers.CharField()
    classification = serializers.CharField()
    gross_per_diem = serializers.FloatField()
    meal_deduction = serializers.FloatField()
    net_per_diem = serializers.FloatField()
    breakfast_provided = serializers.BooleanField()
    lunch_provided = serializers.BooleanField()
    dinner_provided = serializers.BooleanField()


@extend_schema_serializer(component_name="TravelTrip")
class TravelTripSerializer(serializers.Serializer):  # type: ignore[misc]
    """Recorded business travel trip itinerary."""

    id = serializers.CharField()
    title = serializers.CharField()
    destination = serializers.CharField()
    country_code = serializers.CharField()
    started_at = serializers.CharField()
    ended_at = serializers.CharField()
    transport_mode = serializers.CharField()
    distance_km = serializers.FloatField()
    meals_by_date = serializers.DictField(
        child=serializers.DictField(child=serializers.BooleanField())
    )
    entity_id = serializers.CharField(allow_null=True)
    notes = serializers.CharField()
    created_at = serializers.CharField()


@extend_schema_serializer(component_name="TravelTripCalculation")
class TravelTripCalculationSerializer(serializers.Serializer):  # type: ignore[misc]
    """Reimbursement breakdown and calculation for a business travel trip."""

    trip = TravelTripSerializer()
    days = TravelDayAllowanceSerializer(many=True)
    gross_per_diem_total = serializers.FloatField()
    meal_deduction_total = serializers.FloatField()
    net_per_diem_total = serializers.FloatField()
    mileage_allowance = serializers.FloatField()
    total_reimbursement = serializers.FloatField()
    currency = serializers.CharField()


@extend_schema_serializer(component_name="TravelTripListResponse")
class TravelTripListResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `GET /api/v1/reporting/travel-trips/`."""

    trips = TravelTripCalculationSerializer(many=True)
    count = serializers.IntegerField()


# endregion


# region Report Serializers
class JurisdictionReportRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Query parameter specification for statutory jurisdiction reporting."""

    start_date = serializers.CharField(required=True)
    end_date = serializers.CharField(required=True)
    pack = serializers.CharField(default="de", required=False)
    subdivision = serializers.CharField(default="", required=False, allow_blank=True)
    office_one_way_km = serializers.FloatField(default=0.0, required=False)
    report_format = serializers.ChoiceField(
        choices=["json", "csv", "markdown"], default="json", required=False
    )


@extend_schema_serializer(component_name="JurisdictionReportSummary")
class JurisdictionReportSummarySerializer(serializers.Serializer):  # type: ignore[misc]
    """Consolidated statutory tax allowances and business travel reimbursements."""

    pack = ReportingPackSerializer()
    start_date = serializers.CharField()
    end_date = serializers.CharField()
    office_one_way_km = serializers.FloatField()
    home_office_days_raw = serializers.IntegerField()
    home_office_days_eligible = serializers.IntegerField()
    home_office_days_capped = serializers.IntegerField()
    home_office_allowance = serializers.FloatField()
    office_days_raw = serializers.IntegerField()
    office_days_eligible = serializers.IntegerField()
    commute_daily_allowance = serializers.FloatField()
    commute_allowance_total = serializers.FloatField()
    mobile_work_days = serializers.IntegerField()
    client_site_days = serializers.IntegerField()
    excluded_absence_or_holiday_dates = serializers.ListField(child=serializers.CharField())
    excluded_travel_dates = serializers.ListField(child=serializers.CharField())
    trips = TravelTripCalculationSerializer(many=True)
    travel_per_diem_total = serializers.FloatField()
    travel_mileage_total = serializers.FloatField()
    travel_allowance_total = serializers.FloatField()
    absence_summary = serializers.DictField(allow_null=True, required=False)
    total_allowance_and_reimbursement = serializers.FloatField()
    report_format = serializers.CharField(required=False)
    rendered = serializers.CharField(required=False, allow_blank=True)


# endregion
