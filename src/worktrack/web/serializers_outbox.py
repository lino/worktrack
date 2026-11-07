"""DRF serializers for `Outbox`, `PreviewDiff`, `EgressLedger`, OAuth Broker, and `/healthz`."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from worktrack.core import format_utc_iso
from worktrack.outbox import (
    ALLOWED_CHANGE_KINDS,
    ALLOWED_EGRESS_OUTCOMES,
    ALLOWED_FLUSH_CADENCES,
    ALLOWED_FLUSH_TRIGGERS,
    ALLOWED_GROUPING_WINDOWS,
    ALLOWED_OUTBOX_STATUSES,
    ALLOWED_ROUNDING_MODES,
    DEFAULT_FLUSH_CADENCE,
    DEFAULT_GROUPING_WINDOW,
    DEFAULT_MIN_DURATION_SECONDS,
    DEFAULT_REDACTED_SUMMARY_TEMPLATE,
    DEFAULT_ROUNDING_MODE,
    DEFAULT_ROUNDING_QUANTUM_SECONDS,
    EGRESS_OUTCOME_OK,
    FLUSH_TRIGGER_MANUAL,
    EgressRecord,
    FlushResult,
    OutboxItem,
    RollupPolicy,
    RollupPreviewResult,
)
from worktrack.secrets import OAuthTokenBundle

# region Constants
OUTBOX_STATUS_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_OUTBOX_STATUSES)
)
FLUSH_CADENCE_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_FLUSH_CADENCES)
)
FLUSH_TRIGGER_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_FLUSH_TRIGGERS)
)
GROUPING_WINDOW_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_GROUPING_WINDOWS)
)
ROUNDING_MODE_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_ROUNDING_MODES)
)
CHANGE_KIND_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_CHANGE_KINDS)
)
EGRESS_OUTCOME_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_EGRESS_OUTCOMES)
)
DEFAULT_SINK_SLUG = "jira"
# endregion


# region Serializers
class OutboxItemSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `OutboxItem` transport schema for `pinia-orm`."""

    id = serializers.CharField(read_only=True)
    idempotency_key = serializers.CharField(read_only=True)
    sink = serializers.CharField()
    target_ref = serializers.CharField()
    window_start = serializers.DateTimeField()
    window_end = serializers.DateTimeField()
    raw_duration_seconds = serializers.FloatField()
    rounded_duration_seconds = serializers.IntegerField()
    summary = serializers.CharField()
    cadence = serializers.ChoiceField(choices=FLUSH_CADENCE_CHOICES)
    status = serializers.ChoiceField(choices=OUTBOX_STATUS_CHOICES)
    source_interval_ids = serializers.ListField(child=serializers.CharField())
    payload_preview = serializers.DictField()
    payload_sha256 = serializers.CharField()
    attempt_count = serializers.IntegerField()
    rejection_reason = serializers.CharField(allow_null=True)
    last_error = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class PreviewDiffSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `PreviewDiff` component schema for Outbox before/after diffing."""

    sink = serializers.CharField()
    target_ref = serializers.CharField()
    window_start = serializers.DateTimeField()
    window_end = serializers.DateTimeField()
    change_kind = serializers.ChoiceField(choices=CHANGE_KIND_CHOICES)
    before_seconds = serializers.IntegerField(allow_null=True)
    after_seconds = serializers.IntegerField()
    delta_seconds = serializers.IntegerField()
    before_summary = serializers.CharField(allow_null=True)
    after_summary = serializers.CharField()
    idempotency_key = serializers.CharField()
    previous_outbox_item_id = serializers.CharField(allow_null=True)


class OutboxPreviewRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Payload for `POST /api/v1/outbox/preview/` (dry-run diff or stage)."""

    sink = serializers.CharField(default=DEFAULT_SINK_SLUG)
    stage = serializers.BooleanField(default=False)
    skip_unchanged = serializers.BooleanField(default=True)
    since = serializers.DateTimeField(required=False, allow_null=True, default=None)
    until = serializers.DateTimeField(required=False, allow_null=True, default=None)
    grouping_window = serializers.ChoiceField(
        choices=GROUPING_WINDOW_CHOICES,
        default=DEFAULT_GROUPING_WINDOW,
    )
    min_duration_seconds = serializers.IntegerField(default=DEFAULT_MIN_DURATION_SECONDS)
    rounding_quantum_seconds = serializers.IntegerField(default=DEFAULT_ROUNDING_QUANTUM_SECONDS)
    rounding_mode = serializers.ChoiceField(
        choices=ROUNDING_MODE_CHOICES,
        default=DEFAULT_ROUNDING_MODE,
    )
    redact_notes = serializers.BooleanField(default=True)
    summary_template = serializers.CharField(default=DEFAULT_REDACTED_SUMMARY_TEMPLATE)
    standing_activity_mappings = serializers.DictField(
        child=serializers.CharField(),
        required=False,
        default=dict,
    )
    flush_cadence = serializers.ChoiceField(
        choices=FLUSH_CADENCE_CHOICES,
        default=DEFAULT_FLUSH_CADENCE,
    )


class OutboxPreviewResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response schema for `POST /api/v1/outbox/preview/`."""

    cadence = serializers.ChoiceField(choices=FLUSH_CADENCE_CHOICES)
    grouping_window = serializers.ChoiceField(choices=GROUPING_WINDOW_CHOICES)
    total_intervals_examined = serializers.IntegerField()
    unmapped_interval_count = serializers.IntegerField()
    filtered_short_bucket_count = serializers.IntegerField()
    diffs = PreviewDiffSerializer(many=True)
    staged_items = OutboxItemSerializer(many=True)


class OutboxRejectRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Optional rejection reason payload for `POST /api/v1/outbox/items/{id}/reject/`."""

    reason = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)


class EgressReceiptSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `EgressReceipt` component schema summarizing a successful `Sink` write."""

    destination_uri = serializers.CharField()
    http_status = serializers.IntegerField()
    external_receipt_id = serializers.CharField()
    payload_sha256 = serializers.CharField()


class EgressRecordSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `EgressRecord` audit receipt schema for `/api/v1/egress-ledger/`."""

    id = serializers.CharField(read_only=True)
    recorded_at = serializers.DateTimeField()
    outbox_item_id = serializers.CharField()
    idempotency_key = serializers.CharField()
    sink = serializers.CharField()
    destination_uri = serializers.CharField()
    payload_sha256 = serializers.CharField()
    outcome = serializers.ChoiceField(choices=EGRESS_OUTCOME_CHOICES)
    http_status = serializers.IntegerField(allow_null=True)
    external_receipt_id = serializers.CharField(allow_null=True)
    trace_id = serializers.CharField()
    error_detail = serializers.CharField(allow_null=True)


class OutboxFlushRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Payload for `POST /api/v1/outbox/flush/`."""

    item_id = serializers.CharField(required=False, allow_null=True, default=None)
    sink = serializers.CharField(required=False, allow_null=True, default=None)
    cadence = serializers.ChoiceField(
        choices=FLUSH_CADENCE_CHOICES,
        required=False,
        allow_null=True,
        default=None,
    )
    trigger = serializers.ChoiceField(choices=FLUSH_TRIGGER_CHOICES, default=FLUSH_TRIGGER_MANUAL)
    now = serializers.DateTimeField(required=False, allow_null=True, default=None)


class FlushItemResultSerializer(serializers.Serializer):  # type: ignore[misc]
    """Single item flush outcome pairing `OutboxItem`, `EgressRecord`, and `EgressReceipt`."""

    item = OutboxItemSerializer()
    outcome = serializers.ChoiceField(choices=EGRESS_OUTCOME_CHOICES)
    egress_record = EgressRecordSerializer()
    receipt = EgressReceiptSerializer(allow_null=True)


class OutboxFlushResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Response schema for `POST /api/v1/outbox/flush/`."""

    results = FlushItemResultSerializer(many=True)


class OAuthExchangeRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Confidential OAuth 2.0 PKCE code exchange request payload."""

    code = serializers.CharField()
    code_verifier = serializers.CharField()
    redirect_uri = serializers.CharField()


class OAuthRefreshRequestSerializer(serializers.Serializer):  # type: ignore[misc]
    """Confidential OAuth 2.0 refresh-token grant request payload."""

    refresh_token = serializers.CharField()


class OAuthTokenResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Ephemeral `no-store` OAuth token response returned directly to workstation client."""

    access_token = serializers.CharField()
    refresh_token = serializers.CharField(allow_null=True)
    expires_in = serializers.IntegerField(allow_null=True)
    token_type = serializers.CharField()
    scope = serializers.CharField(allow_null=True)


class HealthzResponseSerializer(serializers.Serializer):  # type: ignore[misc]
    """Readiness response schema for `GET /healthz`."""

    status = serializers.CharField()
    mode = serializers.CharField()
    namespace = serializers.CharField()


# endregion


# region Rendering
def outbox_item_to_dict(item: OutboxItem) -> dict[str, object]:
    """Serialize an `OutboxItem` domain dataclass into a JSON-ready dictionary."""
    return {
        "id": item.id,
        "idempotency_key": item.idempotency_key,
        "sink": item.sink,
        "target_ref": item.target_ref,
        "window_start": format_utc_iso(item.window_start),
        "window_end": format_utc_iso(item.window_end),
        "raw_duration_seconds": item.raw_duration_seconds,
        "rounded_duration_seconds": item.rounded_duration_seconds,
        "summary": item.summary,
        "cadence": item.cadence,
        "status": item.status,
        "source_interval_ids": list(item.source_interval_ids),
        "payload_preview": dict(item.payload_preview),
        "payload_sha256": item.payload_sha256,
        "attempt_count": item.attempt_count,
        "rejection_reason": item.rejection_reason,
        "last_error": item.last_error,
        "created_at": format_utc_iso(item.created_at),
        "updated_at": format_utc_iso(item.updated_at),
    }


def egress_record_to_dict(record: EgressRecord) -> dict[str, object]:
    """Serialize an `EgressRecord` domain dataclass into a JSON-ready dictionary."""
    return {
        "id": record.id,
        "recorded_at": format_utc_iso(record.recorded_at),
        "outbox_item_id": record.outbox_item_id,
        "idempotency_key": record.idempotency_key,
        "sink": record.sink,
        "destination_uri": record.destination_uri,
        "payload_sha256": record.payload_sha256,
        "outcome": record.outcome,
        "http_status": record.http_status,
        "external_receipt_id": record.external_receipt_id,
        "trace_id": record.trace_id,
        "error_detail": record.error_detail,
    }


def flush_result_to_dict(result: FlushResult) -> dict[str, object]:
    """Serialize a `FlushResult` with optional `EgressReceipt` summary."""
    rec = result.egress_record
    receipt: dict[str, object] | None = None
    if (
        result.outcome == EGRESS_OUTCOME_OK
        and rec.http_status is not None
        and rec.external_receipt_id is not None
    ):
        receipt = {
            "destination_uri": rec.destination_uri,
            "http_status": rec.http_status,
            "external_receipt_id": rec.external_receipt_id,
            "payload_sha256": rec.payload_sha256,
        }
    return {
        "item": outbox_item_to_dict(result.item),
        "outcome": result.outcome,
        "egress_record": egress_record_to_dict(rec),
        "receipt": receipt,
    }


def preview_result_to_dict(
    preview: RollupPreviewResult,
    staged_items: list[OutboxItem],
) -> dict[str, object]:
    """Serialize `RollupPreviewResult` and optional `staged_items` list."""
    return {
        "cadence": preview.cadence,
        "grouping_window": preview.grouping_window,
        "total_intervals_examined": preview.total_intervals_examined,
        "unmapped_interval_count": preview.unmapped_interval_count,
        "filtered_short_bucket_count": preview.filtered_short_bucket_count,
        "diffs": [diff.to_dict() for diff in preview.diffs],
        "staged_items": [outbox_item_to_dict(item) for item in staged_items],
    }


def build_rollup_policy_from_data(data: dict[str, Any]) -> RollupPolicy:
    """Construct a `RollupPolicy` from validated `OutboxPreviewRequestSerializer` data."""
    return RollupPolicy(
        grouping_window=str(data["grouping_window"]),
        min_duration_seconds=int(data["min_duration_seconds"]),
        rounding_quantum_seconds=int(data["rounding_quantum_seconds"]),
        rounding_mode=str(data["rounding_mode"]),
        redact_notes=bool(data["redact_notes"]),
        summary_template=str(data["summary_template"]),
        standing_activity_mappings=dict(data.get("standing_activity_mappings") or {}),
        flush_cadence=str(data["flush_cadence"]),
    )


def token_bundle_to_dict(bundle: OAuthTokenBundle) -> dict[str, object]:
    """Unwrap ephemeral `OAuthTokenBundle` solely for immediate `no-store` JSON delivery."""
    return {
        "access_token": bundle.access_token.expose(),
        "refresh_token": bundle.refresh_token.expose()
        if bundle.refresh_token is not None
        else None,
        "expires_in": bundle.expires_in,
        "token_type": bundle.token_type,
        "scope": bundle.scope,
    }


# endregion
