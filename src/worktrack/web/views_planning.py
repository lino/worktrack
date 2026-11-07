"""DRF `APIView` endpoints for Horizon Planning (`AgendaItem`, `FocusBlock`, `Insights`)."""

from __future__ import annotations

from datetime import date, datetime

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from worktrack.agenda import DEFAULT_APPROACHING_DEADLINE_HOURS
from worktrack.core import UNSET, LedgerValidationError, UnsetType
from worktrack.web.serializers_planning import (
    AgendaItemSerializer,
    AgendaItemUpdateSerializer,
    FocusBlockSerializer,
    NowInsightSerializer,
    TodayInsightSerializer,
    WeekInsightSerializer,
)
from worktrack.web.services import get_active_web_services
from worktrack.web.views_core import optional_str_param, parse_bool_param, parse_dt_param


# region Validation
def _parse_approaching_hours(request: Request) -> int:
    """Parse optional `approaching_deadline_hours` positive integer query parameter."""
    raw = request.query_params.get("approaching_deadline_hours")
    if raw is None or not raw.strip():
        return DEFAULT_APPROACHING_DEADLINE_HOURS
    try:
        parsed = int(raw.strip())
    except ValueError as exc:
        raise LedgerValidationError("'approaching_deadline_hours' must be an integer.") from exc
    if parsed < 1:
        raise LedgerValidationError("'approaching_deadline_hours' must be >= 1.")
    return parsed


# endregion


# region Views
class AgendaItemListCreateView(APIView):  # type: ignore[misc]
    """List or schedule prioritized `AgendaItems` (`/api/v1/agenda/items/`)."""

    @extend_schema(
        tags=["Agenda"],
        parameters=[
            OpenApiParameter("horizon", OpenApiTypes.STR, required=False),
            OpenApiParameter("status", OpenApiTypes.STR, required=False),
            OpenApiParameter("include_done", OpenApiTypes.BOOL, required=False),
            OpenApiParameter("activity_id", OpenApiTypes.STR, required=False),
        ],
        responses={200: AgendaItemSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """List `AgendaItems` ordered by priority, deadline, and creation timestamp."""
        include_done = parse_bool_param(request, "include_done", default=True)
        services = get_active_web_services()
        with services.open_ledger() as ledger, services.open_planner(ledger=ledger) as planner:
            items = planner.list_items(
                horizon=optional_str_param(request, "horizon"),
                status=optional_str_param(request, "status"),
                include_done=include_done,
                activity_id=optional_str_param(request, "activity_id"),
            )
        return Response([item.to_dict() for item in items])

    @extend_schema(
        tags=["Agenda"],
        request=AgendaItemSerializer,
        responses={201: AgendaItemSerializer},
    )
    def post(self, request: Request) -> Response:
        """Schedule a new `AgendaItem` bound to an `Activity`."""
        serializer = AgendaItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services = get_active_web_services()
        with services.open_ledger() as ledger, services.open_planner(ledger=ledger) as planner:
            ledger.get_activity(str(data["activity_id"]))
            item = planner.schedule_item(
                str(data["activity_id"]),
                horizon=str(data["horizon"]),
                priority=int(data["priority"]),
                status=str(data["status"]),
                budget_seconds=data.get("budget_seconds"),
                daily_budget_minutes=data.get("daily_budget_minutes"),
                deadline=data.get("deadline"),
                notes=data.get("notes"),
            )
        return Response(item.to_dict(), status=status.HTTP_201_CREATED)


class AgendaItemDetailView(APIView):  # type: ignore[misc]
    """Retrieve or update an `AgendaItem` (`/api/v1/agenda/items/{item_id}/`)."""

    @extend_schema(tags=["Agenda"], responses={200: AgendaItemSerializer})
    def get(self, _request: Request, item_id: str) -> Response:
        """Fetch an `AgendaItem` by identifier."""
        with get_active_web_services().open_planner() as planner:
            item = planner.get_item(item_id)
        return Response(item.to_dict())

    @extend_schema(
        tags=["Agenda"],
        request=AgendaItemUpdateSerializer,
        responses={200: AgendaItemSerializer},
    )
    def patch(self, request: Request, item_id: str) -> Response:
        """Partially update mutable fields on an existing `AgendaItem`."""
        serializer = AgendaItemUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        budget_sec: int | UnsetType | None = data.get("budget_seconds", UNSET)
        budget_min: int | UnsetType | None = data.get("daily_budget_minutes", UNSET)
        deadline_val: str | date | datetime | UnsetType | None = data.get("deadline", UNSET)
        notes_val: str | UnsetType | None = data.get("notes", UNSET)
        with get_active_web_services().open_planner() as planner:
            updated = planner.update_item(
                item_id,
                horizon=data.get("horizon"),
                priority=data.get("priority"),
                status=data.get("status"),
                budget_seconds=budget_sec,
                daily_budget_minutes=budget_min,
                deadline=deadline_val,
                notes=notes_val,
            )
        return Response(updated.to_dict())


class FocusBlockListCreateView(APIView):  # type: ignore[misc]
    """List or schedule timeboxed `FocusBlocks` (`/api/v1/agenda/focus-blocks/`)."""

    @extend_schema(
        tags=["Agenda"],
        parameters=[
            OpenApiParameter("since", OpenApiTypes.DATETIME, required=False),
            OpenApiParameter("until", OpenApiTypes.DATETIME, required=False),
            OpenApiParameter("activity_id", OpenApiTypes.STR, required=False),
        ],
        responses={200: FocusBlockSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """List `FocusBlocks` intersecting optional `[since, until)` bounds."""
        since = parse_dt_param(request, "since")
        until = parse_dt_param(request, "until")
        with get_active_web_services().open_planner() as planner:
            blocks = planner.list_focus_blocks(
                since=since,
                until=until,
                activity_id=optional_str_param(request, "activity_id"),
            )
        return Response([block.to_dict() for block in blocks])

    @extend_schema(
        tags=["Agenda"],
        request=FocusBlockSerializer,
        responses={201: FocusBlockSerializer},
    )
    def post(self, request: Request) -> Response:
        """Schedule a new `FocusBlock` with anti-surveillance `expose_busy` toggle."""
        serializer = FocusBlockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services = get_active_web_services()
        with services.open_ledger() as ledger, services.open_planner(ledger=ledger) as planner:
            act_id = data.get("activity_id")
            if act_id is not None:
                ledger.get_activity(str(act_id))
            block = planner.schedule_focus_block(
                started_at=data["started_at"],
                ended_at=data["ended_at"],
                label=data.get("label"),
                activity_id=act_id,
                expose_busy=bool(data.get("expose_busy", False)),
                notes=data.get("notes"),
            )
        return Response(block.to_dict(), status=status.HTTP_201_CREATED)


class FocusBlockDetailView(APIView):  # type: ignore[misc]
    """Retrieve or delete a scheduled `FocusBlock` (`/api/v1/agenda/focus-blocks/{block_id}/`)."""

    @extend_schema(tags=["Agenda"], responses={200: FocusBlockSerializer})
    def get(self, _request: Request, block_id: str) -> Response:
        """Fetch a `FocusBlock` by identifier."""
        with get_active_web_services().open_planner() as planner:
            block = planner.get_focus_block(block_id)
        return Response(block.to_dict())

    @extend_schema(tags=["Agenda"], responses={204: None})
    def delete(self, _request: Request, block_id: str) -> Response:
        """Delete a scheduled `FocusBlock`."""
        with get_active_web_services().open_planner() as planner:
            planner.delete_focus_block(block_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class NowInsightView(APIView):  # type: ignore[misc]
    """Compute `NowInsight` snapshot (`/api/v1/insights/now/`)."""

    @extend_schema(
        tags=["Insights"],
        parameters=[
            OpenApiParameter("now", OpenApiTypes.DATETIME, required=False),
            OpenApiParameter("approaching_deadline_hours", OpenApiTypes.INT, required=False),
        ],
        responses={200: NowInsightSerializer},
    )
    def get(self, request: Request) -> Response:
        """Return point-in-time `NowInsight` ('what am I doing right now?')."""
        now_dt = parse_dt_param(request, "now")
        hours = _parse_approaching_hours(request)
        services = get_active_web_services()
        with services.open_ledger() as ledger, services.open_planner(ledger=ledger) as planner:
            insight = planner.get_now_insight(
                now=now_dt,
                ledger=ledger,
                approaching_deadline_hours=hours,
            )
        return Response(insight.to_dict())


class TodayInsightView(APIView):  # type: ignore[misc]
    """Compute `TodayInsight` horizon snapshot (`/api/v1/insights/today/`)."""

    @extend_schema(
        tags=["Insights"],
        parameters=[
            OpenApiParameter("now", OpenApiTypes.DATETIME, required=False),
            OpenApiParameter("approaching_deadline_hours", OpenApiTypes.INT, required=False),
        ],
        responses={200: TodayInsightSerializer},
    )
    def get(self, request: Request) -> Response:
        """Return aggregated `TodayInsight` ('what am I doing today?')."""
        now_dt = parse_dt_param(request, "now")
        hours = _parse_approaching_hours(request)
        services = get_active_web_services()
        with services.open_ledger() as ledger, services.open_planner(ledger=ledger) as planner:
            insight = planner.get_today_insight(
                now=now_dt,
                ledger=ledger,
                approaching_deadline_hours=hours,
            )
        return Response(insight.to_dict())


class WeekInsightView(APIView):  # type: ignore[misc]
    """Compute `WeekInsight` horizon snapshot (`/api/v1/insights/week/`)."""

    @extend_schema(
        tags=["Insights"],
        parameters=[
            OpenApiParameter("now", OpenApiTypes.DATETIME, required=False),
            OpenApiParameter("approaching_deadline_hours", OpenApiTypes.INT, required=False),
        ],
        responses={200: WeekInsightSerializer},
    )
    def get(self, request: Request) -> Response:
        """Return aggregated `WeekInsight` ('what am I doing this week?')."""
        now_dt = parse_dt_param(request, "now")
        hours = _parse_approaching_hours(request)
        services = get_active_web_services()
        with services.open_ledger() as ledger, services.open_planner(ledger=ledger) as planner:
            insight = planner.get_week_insight(
                now=now_dt,
                ledger=ledger,
                approaching_deadline_hours=hours,
            )
        return Response(insight.to_dict())


# endregion
