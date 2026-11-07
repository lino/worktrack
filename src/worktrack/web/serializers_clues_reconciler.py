"""DRF serializers for Wave 2-3 `Clue`, `Sensor`, and `Reconciler`/`RetroSplit` routes."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from worktrack.collectors.models import Clue, CollectorSyncSummary
from worktrack.reconciler.models import (
    ALLOWED_RETROSPLIT_STRATEGIES,
    RETROSPLIT_STRATEGY_PROPORTIONAL,
    PlausibilityReport,
    RetroSplitProposal,
    RetroSplitResult,
)
from worktrack.sensors.models import WorkplaceSuggestion
from worktrack.web.serializers_core import ActivitySerializer, IntervalSerializer


# region Serializers
@extend_schema_serializer(component_name="Clue")
class ClueSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical OpenAPI 3.1 schema for a persisted `Clue` record."""

    id = serializers.CharField(read_only=True)
    source = serializers.CharField()
    external_key = serializers.CharField()
    external_id = serializers.CharField()
    occurred_at = serializers.CharField()
    summary = serializers.CharField()
    inferred_activity_key = serializers.CharField(allow_null=True, required=False)
    activity_id = serializers.CharField(allow_null=True, required=False)
    confidence = serializers.FloatField()
    metadata = serializers.DictField(default=dict)
    created_at = serializers.CharField(read_only=True)


class ClueListSerializer(serializers.Serializer):  # type: ignore[misc]
    """Envelope listing `Clue` items."""

    clues = ClueSerializer(many=True)


class ClueGitSyncItemSerializer(serializers.Serializer):  # type: ignore[misc]
    """Direct clue item payload submitted to `POST /api/v1/clues/git-sync/`."""

    source = serializers.CharField(default="git", required=False)
    external_id = serializers.CharField(required=False, allow_null=True)
    external_key = serializers.CharField(required=False, allow_null=True)
    occurred_at = serializers.CharField()
    summary = serializers.CharField()
    inferred_activity_key = serializers.CharField(required=False, allow_null=True)
    activity_id = serializers.CharField(required=False, allow_null=True)
    confidence = serializers.FloatField(default=0.85, required=False)
    metadata = serializers.DictField(default=dict, required=False)


class ClueGitSyncRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request body for `POST /api/v1/clues/git-sync/`."""

    repo_path = serializers.CharField(required=False, allow_null=True)
    since = serializers.CharField(required=False, allow_null=True)
    until = serializers.CharField(required=False, allow_null=True)
    author = serializers.CharField(required=False, allow_null=True)
    clues = ClueGitSyncItemSerializer(many=True, required=False)


@extend_schema_serializer(component_name="ClueSyncResult")
class ClueGitSyncResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `POST /api/v1/clues/git-sync/`."""

    commits_scanned = serializers.IntegerField()
    forge_records_scanned = serializers.IntegerField()
    clues_upserted = serializers.IntegerField()
    activities_linked = serializers.IntegerField()
    clues = ClueSerializer(many=True)


@extend_schema_serializer(component_name="WorkplaceSensorRule")
class WorkplaceSensorRuleSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for an ordered `WorkplaceSensorRule`."""

    rule_id = serializers.CharField()
    workplace = serializers.CharField()
    cidrs = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    ipv4_cidrs = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    ipv6_cidrs = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    wifi_ssids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    vpn_interface_prefixes = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    display_count = serializers.IntegerField(required=False, allow_null=True)
    min_displays = serializers.IntegerField(required=False, allow_null=True)
    max_displays = serializers.IntegerField(required=False, allow_null=True)
    require_vpn = serializers.BooleanField(required=False, allow_null=True)
    confidence = serializers.FloatField(default=0.9, required=False)
    description = serializers.CharField(default="", required=False, allow_blank=True)


@extend_schema_serializer(component_name="WorkstationSnapshot")
class WorkstationSnapshotSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for a local `WorkstationSnapshot`."""

    captured_at = serializers.CharField(required=False, allow_null=True)
    ip_addresses = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    ipv4_addresses = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    wifi_ssid = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    active_interfaces = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    vpn_interfaces = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    display_count = serializers.IntegerField(required=False, allow_null=True)
    displays = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    hostname = serializers.CharField(required=False, allow_null=True, allow_blank=True)


@extend_schema_serializer(component_name="WorkplaceSuggestion")
class WorkplaceSuggestionSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for an evaluated `WorkplaceSuggestion`."""

    scope_date = serializers.CharField()
    workplace = serializers.CharField()
    rule_id = serializers.CharField()
    confidence = serializers.FloatField()
    matched_signals = serializers.ListField(child=serializers.CharField())
    snapshot = WorkstationSnapshotSerializer()
    clue_id = serializers.CharField(allow_null=True)
    context_fact_id = serializers.CharField(allow_null=True)


class SensorEvaluateRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request body for `POST /api/v1/sensors/evaluate/`."""

    scope_date = serializers.CharField(required=False, allow_null=True)
    apply = serializers.BooleanField(default=False, required=False)
    overwrite_manual = serializers.BooleanField(default=False, required=False)
    rules = WorkplaceSensorRuleSerializer(many=True, required=False, default=list)
    snapshot = WorkstationSnapshotSerializer(required=False, allow_null=True)


@extend_schema_serializer(component_name="SensorEvaluationResult")
class SensorEvaluateResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `POST /api/v1/sensors/evaluate/`."""

    matched = serializers.BooleanField()
    suggestion = WorkplaceSuggestionSerializer(allow_null=True)


@extend_schema_serializer(component_name="PlausibilityGap")
class PlausibilityGapSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for a single `PlausibilityGap` detected by `PlausibilityReconciler`."""

    activity = ActivitySerializer()
    booked_seconds = serializers.IntegerField()
    min_booked_seconds = serializers.IntegerField()
    deficit_seconds = serializers.IntegerField()
    reasons = serializers.ListField(child=serializers.CharField())
    clues = serializers.ListField(child=serializers.DictField())
    agenda_item_ids = serializers.ListField(child=serializers.CharField())
    suggested_weight = serializers.FloatField()


@extend_schema_serializer(component_name="ReconcileInspectionResult")
class ReconcilerInspectResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `GET /api/v1/reconciler/inspect/`."""

    window_start = serializers.CharField()
    window_end = serializers.CharField()
    min_booked_seconds = serializers.IntegerField()
    has_gaps = serializers.BooleanField()
    has_candidates = serializers.BooleanField()
    total_unallocated_seconds = serializers.IntegerField()
    total_chaotic_seconds = serializers.IntegerField()
    gaps = PlausibilityGapSerializer(many=True)
    unbooked_activities = PlausibilityGapSerializer(many=True)
    candidate_intervals = IntervalSerializer(many=True)
    unallocated_intervals = IntervalSerializer(many=True)
    candidate_clues = ClueSerializer(many=True)


@extend_schema_serializer(component_name="RetroSplitSliceProposal")
class RetroSplitSliceProposalSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for a single proposed slice in a `RetroSplitProposal`."""

    interval_id = serializers.CharField()
    activity_id = serializers.CharField()
    activity_name = serializers.CharField()
    started_at = serializers.CharField()
    ended_at = serializers.CharField()
    duration_seconds = serializers.IntegerField()
    weight = serializers.FloatField()
    share = serializers.FloatField()
    notes = serializers.CharField(allow_null=True)
    tags = serializers.ListField(child=serializers.CharField())


@extend_schema_serializer(component_name="RetroSplitProposal")
class RetroSplitProposalSerializer(serializers.Serializer):  # type: ignore[misc]
    """Schema for a `RetroSplitProposal`."""

    proposal_id = serializers.CharField()
    strategy = serializers.ChoiceField(choices=sorted(ALLOWED_RETROSPLIT_STRATEGIES))
    interval_ids = serializers.ListField(child=serializers.CharField())
    total_duration_seconds = serializers.IntegerField()
    allocated_duration_seconds = serializers.IntegerField()
    is_conserved = serializers.BooleanField()
    normalized_weights = serializers.DictField(child=serializers.FloatField())
    slices = RetroSplitSliceProposalSerializer(many=True)
    created_at = serializers.CharField()


class ReconcilerProposeRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Request body for `POST /api/v1/reconciler/{propose,apply}/`."""

    interval_id = serializers.CharField()
    activity_ids = serializers.ListField(child=serializers.CharField(), min_length=1, required=True)
    strategy = serializers.ChoiceField(
        choices=sorted(ALLOWED_RETROSPLIT_STRATEGIES),
        default=RETROSPLIT_STRATEGY_PROPORTIONAL,
        required=False,
    )
    weights = serializers.DictField(child=serializers.FloatField(), required=False, allow_null=True)


class ReconcilerProposeResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response envelope for `POST /api/v1/reconciler/propose/`."""

    proposal = RetroSplitProposalSerializer()


@extend_schema_serializer(component_name="RetroSplitApplyResult")
class ReconcilerApplyResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response payload for `POST /api/v1/reconciler/apply/`."""

    proposal_id = serializers.CharField()
    strategy = serializers.ChoiceField(choices=sorted(ALLOWED_RETROSPLIT_STRATEGIES))
    total_duration_seconds = serializers.IntegerField()
    interval_count = serializers.IntegerField()
    intervals = IntervalSerializer(many=True)
    created_intervals = IntervalSerializer(many=True)
    applied_at = serializers.CharField()


# endregion


# region Rendering
def serialize_clue_dict(clue: Clue) -> dict[str, Any]:
    """Serialize a `Clue` into a JSON dictionary including `external_id` alias."""
    base = clue.to_dict()
    return {**base, "external_id": clue.external_key}


def serialize_sync_summary_dict(summary: CollectorSyncSummary) -> dict[str, Any]:
    """Serialize a `CollectorSyncSummary` into a JSON dictionary."""
    return {
        "commits_scanned": summary.commits_scanned,
        "forge_records_scanned": summary.forge_records_scanned,
        "clues_upserted": summary.clues_upserted,
        "activities_linked": summary.activities_linked,
        "clues": [serialize_clue_dict(c) for c in summary.clues],
    }


def serialize_sensor_eval(suggestion: WorkplaceSuggestion | None) -> dict[str, Any]:
    """Serialize a `WorkplaceSuggestion | None` into `SensorEvaluateResponseSerializer` shape."""
    if suggestion is None:
        return {"matched": False, "suggestion": None}
    return {"matched": True, "suggestion": suggestion.to_dict()}


def serialize_reconciler_inspect(
    report: PlausibilityReport,
    clues: list[Clue],
) -> dict[str, Any]:
    """Serialize `PlausibilityReport` and candidate `Clue` list for reconciler inspect."""
    rep = report.to_dict()
    return {
        **rep,
        "unbooked_activities": rep["gaps"],
        "unallocated_intervals": rep["candidate_intervals"],
        "candidate_clues": [serialize_clue_dict(c) for c in clues],
    }


def serialize_retrosplit_apply(result: RetroSplitResult) -> dict[str, Any]:
    """Serialize `RetroSplitResult` including `created_intervals` alias."""
    res = result.to_dict()
    return {**res, "created_intervals": res["intervals"]}


def serialize_retrosplit_proposal(proposal: RetroSplitProposal) -> dict[str, Any]:
    """Serialize `RetroSplitProposal` into a response envelope."""
    return {"proposal": proposal.to_dict()}


# endregion
