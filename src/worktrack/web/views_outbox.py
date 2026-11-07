"""DRF `APIView` endpoints for `Outbox` staging/approval/flush and read-only `EgressLedger`."""

from __future__ import annotations

from typing import ClassVar

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from worktrack.outbox import CHANGE_KIND_UNCHANGED, OutboxItem, RollupProposal
from worktrack.web.serializers_outbox import (
    EgressRecordSerializer,
    OutboxFlushRequestSerializer,
    OutboxFlushResponseSerializer,
    OutboxItemSerializer,
    OutboxPreviewRequestSerializer,
    OutboxPreviewResponseSerializer,
    OutboxRejectRequestSerializer,
    build_rollup_policy_from_data,
    egress_record_to_dict,
    flush_result_to_dict,
    outbox_item_to_dict,
    preview_result_to_dict,
)
from worktrack.web.services import get_active_web_services
from worktrack.web.views_core import optional_str_param


# region Views
class OutboxItemListView(APIView):  # type: ignore[misc]
    """List staged, approved, rejected, or flushed `OutboxItems` (`/api/v1/outbox/items/`)."""

    @extend_schema(
        tags=["Outbox"],
        parameters=[
            OpenApiParameter("status", OpenApiTypes.STR, required=False),
            OpenApiParameter("sink", OpenApiTypes.STR, required=False),
            OpenApiParameter("cadence", OpenApiTypes.STR, required=False),
        ],
        responses={200: OutboxItemSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """List `OutboxItems` matching optional `status`, `sink`, and `cadence` filters."""
        with get_active_web_services().open_outbox() as (_ledger, store, _engine, _dispatcher):
            items = store.list_items(
                status=optional_str_param(request, "status"),
                sink=optional_str_param(request, "sink"),
                cadence=optional_str_param(request, "cadence"),
            )
        return Response([outbox_item_to_dict(item) for item in items])


class OutboxItemDetailView(APIView):  # type: ignore[misc]
    """Retrieve a single `OutboxItem` (`/api/v1/outbox/items/{item_id}/`)."""

    @extend_schema(tags=["Outbox"], responses={200: OutboxItemSerializer})
    def get(self, _request: Request, item_id: str) -> Response:
        """Fetch an `OutboxItem` by primary identifier."""
        with get_active_web_services().open_outbox() as (_ledger, store, _engine, _dispatcher):
            item = store.get_item(item_id)
        return Response(outbox_item_to_dict(item))


class OutboxPreviewView(APIView):  # type: ignore[misc]
    """Compute dry-run `PreviewDiff`s and stage `OutboxItems` (`/api/v1/outbox/preview/`)."""

    @extend_schema(
        tags=["Outbox"],
        request=OutboxPreviewRequestSerializer,
        responses={200: OutboxPreviewResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        """Preview `RollupPolicy` diffs against flushed baselines and stage when `stage=True`."""
        serializer = OutboxPreviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        policy = build_rollup_policy_from_data(data)
        sink_name = str(data["sink"])
        should_stage = bool(data["stage"])
        skip_unchanged = bool(data["skip_unchanged"])

        with get_active_web_services().open_outbox() as (_ledger, store, engine, _dispatcher):
            preview = engine.preview_rollup(
                sink=sink_name,
                policy=policy,
                since=data.get("since"),
                until=data.get("until"),
            )
            staged: list[OutboxItem] = []
            if should_stage:
                actionable: list[RollupProposal] = [
                    prop
                    for prop, diff in zip(preview.proposals, preview.diffs, strict=True)
                    if not (skip_unchanged and diff.change_kind == CHANGE_KIND_UNCHANGED)
                ]
                staged = store.stage_proposals(actionable)
        return Response(preview_result_to_dict(preview, staged))


class OutboxApproveView(APIView):  # type: ignore[misc]
    """Approve a staged or failed `OutboxItem` (`/api/v1/outbox/items/{item_id}/approve/`)."""

    @extend_schema(
        tags=["Outbox"],
        request=None,
        responses={200: OutboxItemSerializer},
    )
    def post(self, _request: Request, item_id: str) -> Response:
        """Transition `item_id` into `approved` status."""
        with get_active_web_services().open_outbox() as (_ledger, store, _engine, _dispatcher):
            approved = store.approve_item(item_id)
        return Response(outbox_item_to_dict(approved))


class OutboxRejectView(APIView):  # type: ignore[misc]
    """Reject a staged, approved, or failed `OutboxItem` (`/api/v1/outbox/items/{id}/reject/`)."""

    @extend_schema(
        tags=["Outbox"],
        request=OutboxRejectRequestSerializer,
        responses={200: OutboxItemSerializer},
    )
    def post(self, request: Request, item_id: str) -> Response:
        """Transition `item_id` into `rejected` status with optional `reason`."""
        serializer = OutboxRejectRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_reason = serializer.validated_data.get("reason")
        reason = raw_reason.strip() if isinstance(raw_reason, str) and raw_reason.strip() else None
        with get_active_web_services().open_outbox() as (_ledger, store, _engine, _dispatcher):
            rejected = store.reject_item(item_id, reason=reason)
        return Response(outbox_item_to_dict(rejected))


class OutboxFlushView(APIView):  # type: ignore[misc]
    """Dispatch approved `OutboxItems` to `Sinks` and record SHA-256 `EgressLedger` receipts."""

    @extend_schema(
        tags=["Outbox"],
        request=OutboxFlushRequestSerializer,
        responses={200: OutboxFlushResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        """Flush a single approved `item_id` or all due `approved` items."""
        serializer = OutboxFlushRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        trace_id: str | None = getattr(request, "fritz_trace_id", None)
        trigger = str(data["trigger"])
        explicit_id = data.get("item_id")

        with get_active_web_services().open_outbox() as (_ledger, _store, _engine, dispatcher):
            if explicit_id:
                results = [
                    dispatcher.flush_item(
                        str(explicit_id),
                        trigger=trigger,
                        trace_id=trace_id,
                    )
                ]
            else:
                results = dispatcher.flush_due(
                    now=data.get("now"),
                    trigger=trigger,
                    sink=data.get("sink"),
                    cadence=data.get("cadence"),
                    trace_id=trace_id,
                )
        return Response({"results": [flush_result_to_dict(res) for res in results]})


class EgressLedgerListView(APIView):  # type: ignore[misc]
    """Read-only audit search over append-only `EgressRecord`s (`/api/v1/egress-ledger/`)."""

    http_method_names: ClassVar[list[str]] = ["get", "head", "options"]

    @extend_schema(
        tags=["EgressLedger"],
        parameters=[
            OpenApiParameter("sink", OpenApiTypes.STR, required=False),
            OpenApiParameter("outcome", OpenApiTypes.STR, required=False),
            OpenApiParameter("outbox_item_id", OpenApiTypes.STR, required=False),
        ],
        responses={200: EgressRecordSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """Query immutable `EgressLedger` receipts in chronological order."""
        with get_active_web_services().open_outbox() as (_ledger, store, _engine, _dispatcher):
            records = store.list_egress_records(
                sink=optional_str_param(request, "sink"),
                outcome=optional_str_param(request, "outcome"),
                outbox_item_id=optional_str_param(request, "outbox_item_id"),
            )
        return Response([egress_record_to_dict(rec) for rec in records])


# endregion
