"""DRF `APIView` endpoints for Core Ledger (`Activity` and `Interval` lifecycles)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from worktrack.core import (
    CAPTURE_MODE_CHAOTIC,
    UNSET,
    LedgerValidationError,
    UnsetType,
)
from worktrack.web.serializers_core import (
    ActivitySerializer,
    ActivityUpdateSerializer,
    IntervalBindSerializer,
    IntervalClockInSerializer,
    IntervalClockOutSerializer,
    IntervalCreateSerializer,
    IntervalSerializer,
    IntervalSplitSerializer,
    activity_to_dict,
    build_interval_slice,
    interval_to_dict,
)
from worktrack.web.services import get_active_web_services

# region Constants
TRUTHY_QUERY_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
FALSY_QUERY_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})
# endregion


# region Validation
def parse_bool_param(request: Request, name: str, *, default: bool) -> bool:
    """Parse an optional boolean query parameter from `request.query_params`."""
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return default
    lowered = raw.strip().lower()
    if lowered in TRUTHY_QUERY_VALUES:
        return True
    if lowered in FALSY_QUERY_VALUES:
        return False
    raise LedgerValidationError(f"Query parameter '{name}' must be a boolean.")


def parse_dt_param(request: Request, name: str) -> datetime | None:
    """Parse an optional ISO-8601 timestamp query parameter into a UTC `datetime`."""
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise LedgerValidationError(f"Invalid ISO-8601 query parameter '{name}'.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def optional_str_param(request: Request, name: str) -> str | None:
    """Extract an optional stripped non-empty query string parameter."""
    raw = request.query_params.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped else None


# endregion


# region Views
class ActivityListCreateView(APIView):  # type: ignore[misc]
    """List or create Targeted and Standing `Activities` (`/api/v1/activities/`)."""

    @extend_schema(
        tags=["Activities"],
        parameters=[
            OpenApiParameter("kind", OpenApiTypes.STR, required=False),
            OpenApiParameter("include_archived", OpenApiTypes.BOOL, required=False),
            OpenApiParameter("tag", OpenApiTypes.STR, required=False),
            OpenApiParameter("source", OpenApiTypes.STR, required=False),
        ],
        responses={200: ActivitySerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """List `Activities` matching optional kind, archival, tag, and source filters."""
        include_archived = parse_bool_param(request, "include_archived", default=False)
        with get_active_web_services().open_ledger() as ledger:
            items = ledger.list_activities(
                kind=optional_str_param(request, "kind"),
                include_archived=include_archived,
                tag=optional_str_param(request, "tag"),
                source=optional_str_param(request, "source"),
            )
        return Response([activity_to_dict(item) for item in items])

    @extend_schema(
        tags=["Activities"],
        request=ActivitySerializer,
        responses={201: ActivitySerializer},
    )
    def post(self, request: Request) -> Response:
        """Create a new Targeted or Standing `Activity`."""
        serializer = ActivitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with get_active_web_services().open_ledger() as ledger:
            created = ledger.create_activity(
                str(data["name"]),
                kind=str(data["kind"]),
                external_ref=data.get("external_ref"),
                rollup_target_ref=data.get("rollup_target_ref"),
                source=str(data["source"]),
                tags=list(data.get("tags") or ()),
                archived=bool(data.get("archived", False)),
            )
        return Response(activity_to_dict(created), status=status.HTTP_201_CREATED)


class ActivityDetailView(APIView):  # type: ignore[misc]
    """Retrieve or partially update an `Activity` (`/api/v1/activities/{activity_id}/`)."""

    @extend_schema(tags=["Activities"], responses={200: ActivitySerializer})
    def get(self, _request: Request, activity_id: str) -> Response:
        """Fetch an `Activity` by identifier."""
        with get_active_web_services().open_ledger() as ledger:
            item = ledger.get_activity(activity_id)
        return Response(activity_to_dict(item))

    @extend_schema(
        tags=["Activities"],
        request=ActivityUpdateSerializer,
        responses={200: ActivitySerializer},
    )
    def patch(self, request: Request, activity_id: str) -> Response:
        """Partially update mutable fields on an existing `Activity`."""
        serializer = ActivityUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        ext_ref: str | UnsetType | None = data.get("external_ref", UNSET)
        rollup_ref: str | UnsetType | None = data.get("rollup_target_ref", UNSET)
        with get_active_web_services().open_ledger() as ledger:
            updated = ledger.update_activity(
                activity_id,
                name=data.get("name"),
                external_ref=ext_ref,
                rollup_target_ref=rollup_ref,
                archived=data.get("archived"),
                tags=data.get("tags"),
            )
        return Response(activity_to_dict(updated))


class IntervalListCreateView(APIView):  # type: ignore[misc]
    """Query or record closed structured/chaotic `Intervals` (`/api/v1/intervals/`)."""

    @extend_schema(
        tags=["Intervals"],
        parameters=[
            OpenApiParameter("since", OpenApiTypes.DATETIME, required=False),
            OpenApiParameter("until", OpenApiTypes.DATETIME, required=False),
            OpenApiParameter("activity_id", OpenApiTypes.STR, required=False),
            OpenApiParameter("capture_mode", OpenApiTypes.STR, required=False),
            OpenApiParameter("include_open", OpenApiTypes.BOOL, required=False),
            OpenApiParameter("unallocated_only", OpenApiTypes.BOOL, required=False),
            OpenApiParameter("tag", OpenApiTypes.STR, required=False),
        ],
        responses={200: IntervalSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """List `Intervals` matching optional time window, mode, and tag filters."""
        since = parse_dt_param(request, "since")
        until = parse_dt_param(request, "until")
        include_open = parse_bool_param(request, "include_open", default=True)
        unallocated_only = parse_bool_param(request, "unallocated_only", default=False)
        with get_active_web_services().open_ledger() as ledger:
            intervals = ledger.list_intervals(
                since=since,
                until=until,
                activity_id=optional_str_param(request, "activity_id"),
                capture_mode=optional_str_param(request, "capture_mode"),
                include_open=include_open,
                unallocated_only=unallocated_only,
                tag=optional_str_param(request, "tag"),
            )
        return Response([interval_to_dict(item) for item in intervals])

    @extend_schema(
        tags=["Intervals"],
        request=IntervalCreateSerializer,
        responses={201: IntervalSerializer},
    )
    def post(self, request: Request) -> Response:
        """Record a closed structured or chaotic `Interval`."""
        serializer = IntervalCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        mode = str(data["capture_mode"])
        with get_active_web_services().open_ledger() as ledger:
            if mode == CAPTURE_MODE_CHAOTIC:
                created = ledger.record_chaotic_interval(
                    started_at=data["started_at"],
                    ended_at=data["ended_at"],
                    notes=data.get("notes"),
                    tags=list(data.get("tags") or ()),
                    activity_id=data.get("activity_id"),
                )
            else:
                act_id = data.get("activity_id")
                if not act_id:
                    raise LedgerValidationError(
                        "Structured Intervals require a non-empty 'activity_id'."
                    )
                created = ledger.record_structured_interval(
                    str(act_id),
                    started_at=data["started_at"],
                    ended_at=data["ended_at"],
                    notes=data.get("notes"),
                    tags=list(data.get("tags") or ()),
                )
        return Response(interval_to_dict(created), status=status.HTTP_201_CREATED)


class IntervalDetailView(APIView):  # type: ignore[misc]
    """Retrieve a single `Interval` (`/api/v1/intervals/{interval_id}/`)."""

    @extend_schema(tags=["Intervals"], responses={200: IntervalSerializer})
    def get(self, _request: Request, interval_id: str) -> Response:
        """Fetch an `Interval` by identifier."""
        with get_active_web_services().open_ledger() as ledger:
            interval = ledger.get_interval(interval_id)
        return Response(interval_to_dict(interval))


class IntervalClockInView(APIView):  # type: ignore[misc]
    """Start an active structured `Interval` (`/api/v1/intervals/clock-in/`)."""

    @extend_schema(
        tags=["Intervals"],
        request=IntervalClockInSerializer,
        responses={201: IntervalSerializer},
    )
    def post(self, request: Request) -> Response:
        """Clock in on an `Activity` respecting the requested `overlap_policy`."""
        serializer = IntervalClockInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with get_active_web_services().open_ledger() as ledger:
            interval = ledger.clock_in(
                str(data["activity_id"]),
                started_at=data.get("started_at"),
                notes=data.get("notes"),
                tags=list(data.get("tags") or ()),
                overlap_policy=str(data["overlap_policy"]),
            )
        return Response(interval_to_dict(interval), status=status.HTTP_201_CREATED)


class IntervalClockOutView(APIView):  # type: ignore[misc]
    """Close the active open `Interval` (`/api/v1/intervals/clock-out/`)."""

    @extend_schema(
        tags=["Intervals"],
        request=IntervalClockOutSerializer,
        responses={200: IntervalSerializer},
    )
    def post(self, request: Request) -> Response:
        """Clock out the active `Interval` (or explicit `interval_id`)."""
        serializer = IntervalClockOutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with get_active_web_services().open_ledger() as ledger:
            closed = ledger.clock_out(
                ended_at=data.get("ended_at"),
                interval_id=data.get("interval_id"),
                notes=data.get("notes"),
                tags=data.get("tags"),
            )
        return Response(interval_to_dict(closed))


class IntervalBindView(APIView):  # type: ignore[misc]
    """Bind a chaotic/unallocated `Interval` to an `Activity` (`/api/v1/intervals/{id}/bind/`)."""

    @extend_schema(
        tags=["Intervals"],
        request=IntervalBindSerializer,
        responses={200: IntervalSerializer},
    )
    def post(self, request: Request, interval_id: str) -> Response:
        """Bind `interval_id` to `activity_id`."""
        serializer = IntervalBindSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with get_active_web_services().open_ledger() as ledger:
            bound = ledger.bind_interval(
                interval_id,
                str(data["activity_id"]),
                notes=data.get("notes"),
                tags=data.get("tags"),
                allow_rebind=bool(data.get("allow_rebind", False)),
            )
        return Response(interval_to_dict(bound))


class IntervalSplitView(APIView):  # type: ignore[misc]
    """RetroSplit a closed `Interval` across contiguous slices (`/api/v1/intervals/{id}/split/`)."""

    @extend_schema(
        tags=["Intervals"],
        request=IntervalSplitSerializer,
        responses={200: IntervalSerializer(many=True)},
    )
    def post(self, request: Request, interval_id: str) -> Response:
        """Split `interval_id` into contiguous `Interval` slices."""
        serializer = IntervalSplitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        slice_specs = [build_interval_slice(entry) for entry in serializer.validated_data["slices"]]
        with get_active_web_services().open_ledger() as ledger:
            created_slices = ledger.split_interval(interval_id, slice_specs)
        return Response([interval_to_dict(item) for item in created_slices])


# endregion
