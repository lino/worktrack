"""DRF serializers for Wave 3-4 Calendar, Hub Sync, and Tax/Attribution Export routes."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from worktrack.connectors.calendar.models import (
    DEFAULT_CALENDAR_TARGET_REF,
    DEFAULT_STANDING_MEETINGS_NAME,
    CalendarSyncResult,
)
from worktrack.exporters.models import (
    ALLOWED_EXPORT_FORMATS,
    EXPORT_FORMAT_JSON,
    EntityAttributionReport,
    WorkplaceTaxReport,
)
from worktrack.hub_sync.models import EntityCatalogSnapshot, IngestedHubRollup
from worktrack.outbox.models import OutboxItem
from worktrack.web.serializers_outbox import OutboxItemSerializer, outbox_item_to_dict


# region Serializers
class CalendarSyncRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request body for `POST /api/v1/calendar/sync/`."""

    ics_text = serializers.CharField(required=True)
    calendar_id = serializers.CharField(default=DEFAULT_CALENDAR_TARGET_REF, required=False)
    standing_activity_name = serializers.CharField(
        default=DEFAULT_STANDING_MEETINGS_NAME, required=False
    )
    include_tentative = serializers.BooleanField(default=False, required=False)
    include_all_day = serializers.BooleanField(default=False, required=False)


@extend_schema_serializer(component_name="CalendarSyncResult")
class CalendarSyncResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `POST /api/v1/calendar/sync/`."""

    provider = serializers.CharField()
    events_fetched = serializers.IntegerField()
    events_synced = serializers.IntegerField()
    activities_created = serializers.IntegerField()
    activities_updated = serializers.IntegerField()
    intervals_created = serializers.IntegerField()
    intervals_updated = serializers.IntegerField()
    intervals_skipped = serializers.IntegerField()


class CalendarStageFocusRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request body for `POST /api/v1/calendar/stage-focus/`."""

    start = serializers.CharField(required=False, allow_null=True)
    end = serializers.CharField(required=False, allow_null=True)
    target_ref = serializers.CharField(default=DEFAULT_CALENDAR_TARGET_REF, required=False)
    busy_summary = serializers.CharField(required=False, allow_null=True)
    expose_label = serializers.BooleanField(default=False, required=False)


class CalendarStageFocusResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `POST /api/v1/calendar/stage-focus/`."""

    staged_items = OutboxItemSerializer(many=True)


class HubRollupIngestRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request body for `POST /api/v1/hub/rollups/`."""

    schema = serializers.CharField(required=False, allow_null=True)
    node_id = serializers.CharField(required=False, allow_null=True)
    outbox_item_id = serializers.CharField(required=False, allow_null=True)
    idempotency_key = serializers.CharField()
    target_ref = serializers.CharField()
    grouping_window = serializers.CharField(required=False, allow_null=True)
    window_start = serializers.CharField(required=False, allow_null=True)
    started_at = serializers.CharField(required=False, allow_null=True)
    window_end = serializers.CharField()
    rounded_duration_seconds = serializers.IntegerField(required=False, allow_null=True)
    total_rounded_seconds = serializers.IntegerField(required=False, allow_null=True)
    time_spent_seconds = serializers.IntegerField(required=False, allow_null=True)
    summary = serializers.CharField(required=False, allow_null=True)
    cadence = serializers.CharField(required=False, allow_null=True)
    payload_preview = serializers.DictField(required=False, allow_null=True)
    payload_sha256 = serializers.CharField(required=False, allow_null=True)
    entity_shares = serializers.DictField(
        child=serializers.FloatField(), required=False, default=dict
    )
    entity_shares_seconds = serializers.DictField(
        child=serializers.FloatField(), required=False, default=dict
    )
    buckets = serializers.ListField(child=serializers.DictField(), required=False, default=list)


@extend_schema_serializer(component_name="IngestedHubRollup")
class IngestedHubRollupSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical schema for an ingested `IngestedHubRollup` record."""

    id = serializers.CharField()
    node_id = serializers.CharField()
    outbox_item_id = serializers.CharField()
    idempotency_key = serializers.CharField()
    target_ref = serializers.CharField()
    window_start = serializers.CharField()
    window_end = serializers.CharField()
    rounded_duration_seconds = serializers.IntegerField()
    summary = serializers.CharField()
    cadence = serializers.CharField()
    payload_preview = serializers.DictField()
    payload_sha256 = serializers.CharField()
    entity_shares = serializers.DictField(child=serializers.FloatField())
    ingested_at = serializers.CharField()
    deduplicated = serializers.BooleanField()


class HubRollupListSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response envelope for `GET /api/v1/hub/rollups/`."""

    rollups = IngestedHubRollupSerializer(many=True)


@extend_schema_serializer(component_name="CatalogEntityRecord")
class CatalogEntityRecordSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for a shared `CatalogEntityRecord`."""

    id = serializers.CharField(required=False, allow_null=True)
    kind = serializers.CharField()
    key = serializers.CharField()
    name = serializers.CharField()
    metadata = serializers.DictField(default=dict, required=False)
    updated_at = serializers.CharField(read_only=True, required=False)


@extend_schema_serializer(component_name="CatalogAttributionTemplate")
class CatalogAttributionTemplateSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for a shared `CatalogAttributionTemplate`."""

    id = serializers.CharField(required=False, allow_null=True)
    target_kind = serializers.CharField()
    target_key = serializers.CharField()
    weight = serializers.FloatField()
    source_kind = serializers.CharField(required=False, allow_null=True)
    source_key = serializers.CharField(required=False, allow_null=True)
    source_activity_ref = serializers.CharField(required=False, allow_null=True)
    updated_at = serializers.CharField(read_only=True, required=False)


@extend_schema_serializer(component_name="EntityCatalogSnapshot")
class HubCatalogSnapshotSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical schema for `EntityCatalogSnapshot`."""

    version = serializers.IntegerField()
    generated_at = serializers.CharField()
    entities = CatalogEntityRecordSerializer(many=True)
    attribution_templates = CatalogAttributionTemplateSerializer(many=True)


class HubCatalogUpsertRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request body for `POST /api/v1/hub/catalog/`."""

    entities = CatalogEntityRecordSerializer(many=True, required=False, default=list)
    attribution_templates = CatalogAttributionTemplateSerializer(
        many=True, required=False, default=list
    )


@extend_schema_serializer(component_name="WorkplaceSummaryRow")
class WorkplaceSummaryRowSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for a `WorkplaceSummaryRow` in `WorkplaceTaxReport`."""

    workplace = serializers.CharField()
    day_count = serializers.IntegerField()
    tracked_seconds = serializers.FloatField()
    tracked_hours = serializers.FloatField()
    sources = serializers.ListField(child=serializers.CharField())


@extend_schema_serializer(component_name="WorkplaceDayEntry")
class WorkplaceDayEntrySerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for a daily entry in `WorkplaceTaxReport`."""

    scope_date = serializers.CharField()
    workplace = serializers.CharField()
    source = serializers.CharField()
    tracked_seconds = serializers.FloatField()
    tracked_hours = serializers.FloatField()
    interval_count = serializers.IntegerField()


@extend_schema_serializer(component_name="WorkplaceTaxReport")
class WorkplaceTaxReportSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical schema for `WorkplaceTaxReport`."""

    start_date = serializers.CharField()
    end_date = serializers.CharField()
    total_classified_days = serializers.IntegerField()
    total_unclassified_days = serializers.IntegerField()
    total_tracked_seconds = serializers.FloatField()
    total_tracked_hours = serializers.FloatField()
    days_by_workplace = serializers.DictField(child=serializers.IntegerField())
    workplace_days = serializers.DictField(child=serializers.IntegerField())
    hours_by_workplace = serializers.DictField(child=serializers.FloatField())
    seconds_by_workplace = serializers.DictField(child=serializers.FloatField())
    summary_rows = WorkplaceSummaryRowSerializer(many=True)
    daily_entries = WorkplaceDayEntrySerializer(many=True)


class WorkplaceTaxExportResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `GET /api/v1/exports/workplace-tax/`."""

    format = serializers.ChoiceField(
        choices=sorted(ALLOWED_EXPORT_FORMATS), default=EXPORT_FORMAT_JSON
    )
    report = WorkplaceTaxReportSerializer()
    rendered = serializers.CharField()


@extend_schema_serializer(component_name="EntityStatementRow")
class EntityStatementRowSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for an `EntityStatementRow` in `EntityAttributionReport`."""

    entity_id = serializers.CharField()
    kind = serializers.CharField()
    entity_kind = serializers.CharField()
    key = serializers.CharField()
    entity_key = serializers.CharField()
    name = serializers.CharField()
    attributed_seconds = serializers.FloatField()
    attributed_hours = serializers.FloatField()
    share_of_kind = serializers.FloatField()
    percentage_of_kind = serializers.FloatField()


@extend_schema_serializer(component_name="EntityAttributionReport")
class EntityAttributionReportSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical schema for `EntityAttributionReport`."""

    window_start = serializers.CharField(allow_null=True)
    window_end = serializers.CharField(allow_null=True)
    target_kind = serializers.CharField(allow_null=True)
    normalized = serializers.BooleanField()
    total_interval_seconds = serializers.FloatField()
    total_interval_hours = serializers.FloatField()
    totals_by_kind = serializers.DictField(child=serializers.FloatField())
    hours_by_kind = serializers.DictField(child=serializers.FloatField())
    rows = EntityStatementRowSerializer(many=True)


class EntityAttributionExportResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `GET /api/v1/exports/entity-attribution/`."""

    format = serializers.ChoiceField(
        choices=sorted(ALLOWED_EXPORT_FORMATS), default=EXPORT_FORMAT_JSON
    )
    report = EntityAttributionReportSerializer()
    rendered = serializers.CharField()


# endregion


# region Rendering
def serialize_calendar_sync_result(result: CalendarSyncResult) -> dict[str, Any]:
    """Serialize `CalendarSyncResult` including `events_synced` alias."""
    return {**result.to_dict(), "events_synced": result.events_fetched}


def serialize_staged_focus_items(items: list[OutboxItem]) -> dict[str, Any]:
    """Serialize staged calendar `OutboxItem` list with `payload` alias."""
    rendered: list[dict[str, Any]] = []
    for item in items:
        base = outbox_item_to_dict(item)
        preview = dict(item.payload_preview)
        if "summary" in preview and "busy_summary" not in preview:
            preview["busy_summary"] = preview["summary"]
        rendered.append({**base, "payload": preview})
    return {"staged_items": rendered}


def serialize_ingested_rollup(record: IngestedHubRollup) -> dict[str, Any]:
    """Serialize `IngestedHubRollup` into a JSON dictionary."""
    return record.to_dict()


def serialize_catalog_snapshot(snapshot: EntityCatalogSnapshot) -> dict[str, Any]:
    """Serialize `EntityCatalogSnapshot` into a JSON dictionary."""
    return snapshot.to_dict()


def serialize_workplace_tax_export(
    report: WorkplaceTaxReport,
    rendered: str,
    fmt: str,
) -> dict[str, Any]:
    """Serialize `WorkplaceTaxReport` and rendered text."""
    rep = {**report.to_dict(), "workplace_days": dict(report.days_by_workplace)}
    return {"format": fmt, "report": rep, "rendered": rendered}


def serialize_entity_attribution_export(
    report: EntityAttributionReport,
    rendered: str,
    fmt: str,
) -> dict[str, Any]:
    """Serialize `EntityAttributionReport` and rendered text."""
    raw = report.to_dict()
    rows = [{**r.to_dict(), "entity_key": r.key, "entity_kind": r.kind} for r in report.rows]
    return {"format": fmt, "report": {**raw, "rows": rows}, "rendered": rendered}


# endregion
