"""DRF `APIView` endpoints for Wave 3-4 Calendar, Hub Sync, and Tax/Attribution Exports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from worktrack.cli.context import parse_cli_timestamp
from worktrack.connectors.calendar import (
    DEFAULT_CALENDAR_TARGET_REF,
    DEFAULT_STANDING_MEETINGS_NAME,
    CalendarBucketMapping,
    CalendarFocusBlockStaging,
    IcalCalendarSource,
)
from worktrack.exporters import (
    EXPORT_FORMAT_JSON,
    ExporterValidationError,
    TaxAndAttributionExporter,
)
from worktrack.hub_sync import (
    NODE_ID_HEADER_NAME,
    PAYLOAD_SHA256_HEADER_NAME,
    validate_ganglia_rollup_payload,
)
from worktrack.web.serializers_calendar_hub_exports import (
    CalendarStageFocusRequestSerializer,
    CalendarStageFocusResponseSerializer,
    CalendarSyncRequestSerializer,
    CalendarSyncResponseSerializer,
    EntityAttributionExportResponseSerializer,
    HubCatalogSnapshotSerializer,
    HubCatalogUpsertRequestSerializer,
    HubRollupIngestRequestSerializer,
    HubRollupListSerializer,
    IngestedHubRollupSerializer,
    WorkplaceTaxExportResponseSerializer,
    serialize_calendar_sync_result,
    serialize_catalog_snapshot,
    serialize_entity_attribution_export,
    serialize_ingested_rollup,
    serialize_staged_focus_items,
    serialize_workplace_tax_export,
)
from worktrack.web.services import get_active_web_services
from worktrack.web.views_core import parse_bool_param, parse_dt_param

# region Constants
HOURS_PER_DAY: Final = 24
# endregion


# region Validation
def _normalize_hub_rollup_payload(raw_data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize inbound Hub rollup payload while preserving forbidden-key checks."""
    validate_ganglia_rollup_payload(raw_data)
    normalized = dict(raw_data)
    if normalized.get("rounded_duration_seconds") is None:
        alt_dur = normalized.get("total_rounded_seconds") or normalized.get("time_spent_seconds")
        if alt_dur is not None:
            normalized["rounded_duration_seconds"] = int(alt_dur)
    if not normalized.get("entity_shares") and isinstance(
        normalized.get("entity_shares_seconds"), Mapping
    ):
        raw_sec_map = {
            str(k): float(v)
            for k, v in normalized["entity_shares_seconds"].items()
            if float(v) > 0.0
        }
        total_sec = sum(raw_sec_map.values())
        normalized["entity_shares"] = (
            {k: (v / total_sec if total_sec > 1.0 else v) for k, v in raw_sec_map.items()}
            if total_sec > 0.0
            else {}
        )
    return normalized


# endregion


# region Views
class CalendarSyncView(APIView):  # type: ignore[misc]
    """Sync inline RFC 5545 `.ics` calendar events into `Ledger` via `IcalCalendarSource`."""

    @extend_schema(
        operation_id="v1_calendar_sync",
        request=CalendarSyncRequestSerializer,
        responses={200: CalendarSyncResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        """Import inline `ics_text` into `Ledger`."""
        serializer = CalendarSyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services = get_active_web_services()

        mapping = CalendarBucketMapping(
            default_standing_activity_name=str(
                data.get("standing_activity_name") or DEFAULT_STANDING_MEETINGS_NAME
            )
        )
        source = IcalCalendarSource(
            bucket_mapping=mapping,
            calendar_id=str(data.get("calendar_id") or DEFAULT_CALENDAR_TARGET_REF),
            include_tentative=bool(data.get("include_tentative", False)),
            include_all_day=bool(data.get("include_all_day", False)),
            telemetry=services.telemetry,
        )
        with services.open_ledger() as ledger:
            result = source.sync(ledger, ics_content=str(data["ics_text"]))
        return Response(serialize_calendar_sync_result(result), status=status.HTTP_200_OK)


class CalendarStageFocusView(APIView):  # type: ignore[misc]
    """Stage `expose_busy=True` `FocusBlocks` into `OutboxStore` with opaque redaction."""

    @extend_schema(
        operation_id="v1_calendar_stage_focus",
        request=CalendarStageFocusRequestSerializer,
        responses={201: CalendarStageFocusResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        """Stage eligible `FocusBlocks` in `[start, end)` into `OutboxStore`."""
        serializer = CalendarStageFocusRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services = get_active_web_services()

        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = (
            parse_cli_timestamp(str(data["start"]), field_name="start")
            if data.get("start")
            else today_start
        )
        end_dt = (
            parse_cli_timestamp(str(data["end"]), field_name="end")
            if data.get("end")
            else start_dt + timedelta(hours=HOURS_PER_DAY)
        )
        target_ref = str(data.get("target_ref") or DEFAULT_CALENDAR_TARGET_REF)
        with (
            services.open_planner() as planner,
            services.open_outbox() as (_ledger, outbox_store, _engine, _dispatcher),
        ):
            blocks = planner.list_focus_blocks(since=start_dt, until=end_dt)
            staging = CalendarFocusBlockStaging(
                outbox_store=outbox_store,
                default_target_ref=target_ref,
                expose_label=bool(data.get("expose_label", False)),
            )
            busy_summary = data.get("busy_summary")
            staged = staging.stage_focus_blocks(
                blocks,
                target_ref=target_ref,
                busy_summary=str(busy_summary) if busy_summary else None,
                expose_label=bool(data.get("expose_label", False)),
            )
        return Response(
            serialize_staged_focus_items(staged),
            status=status.HTTP_201_CREATED,
        )


class HubRollupsView(APIView):  # type: ignore[misc]
    """List ingested Node rollups or ingest a `GangliaRollupEnvelope` into the Hub store."""

    @extend_schema(
        operation_id="v1_hub_rollups_list",
        parameters=[
            OpenApiParameter("node_id", OpenApiTypes.STR, description="Filter by node_id."),
            OpenApiParameter("target_ref", OpenApiTypes.STR, description="Filter by target_ref."),
        ],
        responses={200: HubRollupListSerializer},
    )
    def get(self, request: Request) -> Response:
        """List ingested Hub rollups."""
        services = get_active_web_services()
        node_id = request.query_params.get("node_id")
        target_ref = request.query_params.get("target_ref")
        with services.open_hub_ingest_store() as store:
            rollups = store.list_ingested_rollups(
                node_id=node_id.strip() if node_id and node_id.strip() else None,
                target_ref=target_ref.strip() if target_ref and target_ref.strip() else None,
            )
        return Response({"rollups": [serialize_ingested_rollup(r) for r in rollups]})

    @extend_schema(
        operation_id="v1_hub_rollups_ingest",
        request=HubRollupIngestRequestSerializer,
        responses={200: IngestedHubRollupSerializer, 201: IngestedHubRollupSerializer},
    )
    def post(self, request: Request) -> Response:
        """Validate and idempotently ingest an aggregate Node rollup."""
        raw_mapping = request.data if isinstance(request.data, Mapping) else {}
        validate_ganglia_rollup_payload(raw_mapping)
        serializer = HubRollupIngestRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        normalized = _normalize_hub_rollup_payload(raw_mapping)
        services = get_active_web_services()

        header_node = request.headers.get(NODE_ID_HEADER_NAME, "").strip() or None
        header_sha = request.headers.get(PAYLOAD_SHA256_HEADER_NAME, "").strip() or None
        with services.open_hub_ingest_store() as store:
            ingested = store.ingest_rollup(
                normalized,
                node_id=header_node,
                expected_sha256=header_sha,
            )
        http_code = status.HTTP_200_OK if ingested.deduplicated else status.HTTP_201_CREATED
        return Response(serialize_ingested_rollup(ingested), status=http_code)


class HubCatalogView(APIView):  # type: ignore[misc]
    """Export `EntityCatalogSnapshot` or upsert shared catalog entities and templates."""

    @extend_schema(
        operation_id="v1_hub_catalog_get",
        responses={200: HubCatalogSnapshotSerializer},
    )
    def get(self, request: Request) -> Response:
        """Return current `EntityCatalogSnapshot`."""
        _ = request
        services = get_active_web_services()
        with services.open_hub_ingest_store() as store:
            snapshot = store.get_catalog_snapshot()
        return Response(serialize_catalog_snapshot(snapshot), status=status.HTTP_200_OK)

    @extend_schema(
        operation_id="v1_hub_catalog_upsert",
        request=HubCatalogUpsertRequestSerializer,
        responses={200: HubCatalogSnapshotSerializer},
    )
    def post(self, request: Request) -> Response:
        """Upsert shared `Entity` catalog records and default `Attribution` templates."""
        serializer = HubCatalogUpsertRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services = get_active_web_services()

        with services.open_hub_ingest_store() as store:
            for ent in data.get("entities", []):
                store.upsert_catalog_entity(
                    str(ent["kind"]),
                    str(ent["key"]),
                    str(ent["name"]),
                    metadata=dict(ent.get("metadata") or {}),
                    entity_id=ent.get("id"),
                )
            for tmpl in data.get("attribution_templates", []):
                store.upsert_attribution_template(
                    target_kind=str(tmpl["target_kind"]),
                    target_key=str(tmpl["target_key"]),
                    weight=float(tmpl["weight"]),
                    source_kind=tmpl.get("source_kind"),
                    source_key=tmpl.get("source_key"),
                    source_activity_ref=tmpl.get("source_activity_ref"),
                )
            snapshot = store.get_catalog_snapshot()
        return Response(serialize_catalog_snapshot(snapshot), status=status.HTTP_200_OK)


class WorkplaceTaxExportView(APIView):  # type: ignore[misc]
    """Generate annual or custom-window `WorkplaceTaxReport` via `TaxAndAttributionExporter`."""

    @extend_schema(
        operation_id="v1_exports_workplace_tax",
        parameters=[
            OpenApiParameter("year", OpenApiTypes.INT, description="Four-digit calendar year."),
            OpenApiParameter("start_date", OpenApiTypes.DATE, description="Start date YYYY-MM-DD."),
            OpenApiParameter("end_date", OpenApiTypes.DATE, description="End date YYYY-MM-DD."),
            OpenApiParameter(
                "format",
                OpenApiTypes.STR,
                description="Export format (json, csv, or markdown).",
            ),
        ],
        responses={200: WorkplaceTaxExportResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        """Build and render `WorkplaceTaxReport`."""
        services = get_active_web_services()
        raw_year = request.query_params.get("year")
        year: int | None = None
        if raw_year is not None and raw_year.strip():
            try:
                year = int(raw_year.strip())
            except ValueError as exc:
                raise ExporterValidationError("Query parameter 'year' must be an integer.") from exc
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        if year is None and not (start_date and end_date):
            year = datetime.now(UTC).year
        fmt = (request.query_params.get("format") or EXPORT_FORMAT_JSON).strip().lower()
        with services.open_ledger() as ledger:
            exporter = TaxAndAttributionExporter(ledger, telemetry=services.telemetry)
            report = exporter.build_workplace_tax_report(
                year=year,
                start_date=start_date,
                end_date=end_date,
            )
            rendered = exporter.export_workplace_tax_report(
                year=year,
                start_date=start_date,
                end_date=end_date,
                fmt=fmt,
            )
        return Response(
            serialize_workplace_tax_export(report, rendered, fmt),
            status=status.HTTP_200_OK,
        )


class EntityAttributionExportView(APIView):  # type: ignore[misc]
    """Generate `EntityAttributionReport` over `[start, end)` via `TaxAndAttributionExporter`."""

    @extend_schema(
        operation_id="v1_exports_entity_attribution",
        parameters=[
            OpenApiParameter("start", OpenApiTypes.DATETIME, description="Window start UTC."),
            OpenApiParameter("end", OpenApiTypes.DATETIME, description="Window end UTC."),
            OpenApiParameter("entity_kind", OpenApiTypes.STR, description="Entity kind filter."),
            OpenApiParameter("normalize", OpenApiTypes.BOOL, description="Normalize shares."),
            OpenApiParameter(
                "format",
                OpenApiTypes.STR,
                description="Export format (json, csv, or markdown).",
            ),
        ],
        responses={200: EntityAttributionExportResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        """Build and render `EntityAttributionReport`."""
        services = get_active_web_services()
        start_dt = parse_dt_param(request, "start")
        end_dt = parse_dt_param(request, "end")
        entity_kind = request.query_params.get("entity_kind")
        clean_kind = entity_kind.strip() if entity_kind and entity_kind.strip() else None
        normalize = parse_bool_param(request, "normalize", default=False)
        fmt = (request.query_params.get("format") or EXPORT_FORMAT_JSON).strip().lower()
        with services.open_ledger() as ledger:
            exporter = TaxAndAttributionExporter(ledger, telemetry=services.telemetry)
            report = exporter.build_entity_attribution_report(
                since=start_dt,
                until=end_dt,
                target_kind=clean_kind,
                normalize=normalize,
            )
            rendered = exporter.export_entity_attribution_report(
                since=start_dt,
                until=end_dt,
                target_kind=clean_kind,
                normalize=normalize,
                fmt=fmt,
            )
        return Response(
            serialize_entity_attribution_export(report, rendered, fmt),
            status=status.HTTP_200_OK,
        )


# endregion
