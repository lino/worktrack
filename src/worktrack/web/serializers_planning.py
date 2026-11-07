"""DRF serializers for Horizon Planning (`AgendaItem`, `FocusBlock`, `Now/Today/WeekInsight`)."""

from __future__ import annotations

from rest_framework import serializers

from worktrack.agenda import (
    AGENDA_STATUS_PLANNED,
    ALLOWED_AGENDA_HORIZONS,
    ALLOWED_AGENDA_STATUSES,
    DEFAULT_AGENDA_PRIORITY,
    DEFAULT_FOCUS_LABEL,
    HORIZON_TODAY,
    MAX_AGENDA_PRIORITY,
    MIN_AGENDA_PRIORITY,
)
from worktrack.web.serializers_core import (
    ACTIVITY_KIND_CHOICES,
    ActivitySerializer,
    EntityAttributionShareSerializer,
    IntervalSerializer,
)

# region Constants
AGENDA_HORIZON_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_AGENDA_HORIZONS)
)
AGENDA_STATUS_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_AGENDA_STATUSES)
)
# endregion


# region Serializers
class AgendaItemSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `AgendaItem` transport schema for `pinia-orm`."""

    id = serializers.CharField(read_only=True)
    activity_id = serializers.CharField()
    horizon = serializers.ChoiceField(choices=AGENDA_HORIZON_CHOICES, default=HORIZON_TODAY)
    priority = serializers.IntegerField(
        min_value=MIN_AGENDA_PRIORITY,
        max_value=MAX_AGENDA_PRIORITY,
        default=DEFAULT_AGENDA_PRIORITY,
    )
    status = serializers.ChoiceField(choices=AGENDA_STATUS_CHOICES, default=AGENDA_STATUS_PLANNED)
    budget_seconds = serializers.IntegerField(required=False, allow_null=True, default=None)
    daily_budget_minutes = serializers.IntegerField(required=False, allow_null=True, default=None)
    deadline = serializers.CharField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class AgendaItemUpdateSerializer(serializers.Serializer):  # type: ignore[misc]
    """Partial update payload for `PATCH /api/v1/agenda/items/{item_id}/`."""

    horizon = serializers.ChoiceField(choices=AGENDA_HORIZON_CHOICES, required=False)
    priority = serializers.IntegerField(
        min_value=MIN_AGENDA_PRIORITY,
        max_value=MAX_AGENDA_PRIORITY,
        required=False,
    )
    status = serializers.ChoiceField(choices=AGENDA_STATUS_CHOICES, required=False)
    budget_seconds = serializers.IntegerField(required=False, allow_null=True)
    daily_budget_minutes = serializers.IntegerField(required=False, allow_null=True)
    deadline = serializers.CharField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class FocusBlockSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `FocusBlock` transport schema for `pinia-orm`."""

    id = serializers.CharField(read_only=True)
    started_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField()
    duration_seconds = serializers.IntegerField(read_only=True)
    activity_id = serializers.CharField(required=False, allow_null=True, default=None)
    label = serializers.CharField(required=False, default=DEFAULT_FOCUS_LABEL)
    expose_busy = serializers.BooleanField(required=False, default=False)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class ActivityDurationSummarySerializer(serializers.Serializer):  # type: ignore[misc]
    """Tracked duration summary per `Activity` within a horizon window."""

    activity_id = serializers.CharField(allow_null=True)
    activity_key = serializers.CharField()
    activity_name = serializers.CharField()
    activity_kind = serializers.ChoiceField(choices=ACTIVITY_KIND_CHOICES, allow_null=True)
    tracked_seconds = serializers.IntegerField()
    interval_count = serializers.IntegerField()


class AgendaQueueEntrySerializer(serializers.Serializer):  # type: ignore[misc]
    """Enriched `AgendaItem` entry pairing `Activity`, budget remainder, and urgency flags."""

    item = AgendaItemSerializer()
    activity = ActivitySerializer()
    activity_key = serializers.CharField()
    tracked_seconds = serializers.IntegerField()
    remaining_budget_seconds = serializers.IntegerField(allow_null=True)
    deadline_approaching = serializers.BooleanField()
    is_overdue = serializers.BooleanField()


class NowInsightSerializer(serializers.Serializer):  # type: ignore[misc]
    """OpenAPI 3.1 `NowInsight` component schema for `GET /api/v1/insights/now/`."""

    reference_time = serializers.DateTimeField()
    date_iso = serializers.CharField()
    active_interval = IntervalSerializer(allow_null=True)
    active_activity = ActivitySerializer(allow_null=True)
    active_elapsed_seconds = serializers.IntegerField()
    active_focus_block = FocusBlockSerializer(allow_null=True)
    upcoming_focus_block = FocusBlockSerializer(allow_null=True)
    next_agenda_item = AgendaQueueEntrySerializer(allow_null=True)
    today_total_seconds = serializers.IntegerField()
    workplace = serializers.CharField(allow_null=True)


class _BaseHorizonInsightSerializer(serializers.Serializer):  # type: ignore[misc]
    """Shared structural fields for `TodayInsight` and `WeekInsight` schemas."""

    horizon = serializers.ChoiceField(choices=AGENDA_HORIZON_CHOICES)
    reference_time = serializers.DateTimeField()
    date_iso = serializers.CharField()
    window_start = serializers.DateTimeField()
    window_end = serializers.DateTimeField()
    active_interval = IntervalSerializer(allow_null=True)
    active_activity = ActivitySerializer(allow_null=True)
    active_elapsed_seconds = serializers.IntegerField()
    active_focus_block = FocusBlockSerializer(allow_null=True)
    upcoming_focus_block = FocusBlockSerializer(allow_null=True)
    focus_blocks = FocusBlockSerializer(many=True)
    total_tracked_seconds = serializers.IntegerField()
    unallocated_seconds = serializers.IntegerField()
    planned_budget_seconds = serializers.IntegerField()
    activity_totals = ActivityDurationSummarySerializer(many=True)
    agenda_queue = AgendaQueueEntrySerializer(many=True)
    approaching_deadlines = AgendaQueueEntrySerializer(many=True)
    workplace = serializers.CharField(allow_null=True)
    workplace_breakdown = serializers.DictField(child=serializers.IntegerField())
    entity_attributions = EntityAttributionShareSerializer(many=True)


class TodayInsightSerializer(_BaseHorizonInsightSerializer):
    """OpenAPI 3.1 `TodayInsight` component schema for `GET /api/v1/insights/today/`."""


class WeekInsightSerializer(_BaseHorizonInsightSerializer):
    """OpenAPI 3.1 `WeekInsight` component schema for `GET /api/v1/insights/week/`."""


# endregion
