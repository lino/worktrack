"""DRF serializers and mappers for Core Ledger (`Activity`, `Interval`, `Entity`, `Facet`)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from worktrack.core import (
    ACTIVITY_KIND_TARGETED,
    ALLOWED_ACTIVITY_KINDS,
    ALLOWED_CAPTURE_MODES,
    ALLOWED_FACET_VALUE_TYPES,
    ALLOWED_OVERLAP_POLICIES,
    CAPTURE_MODE_STRUCTURED,
    DEFAULT_ACTIVITY_SOURCE,
    DEFAULT_CONTEXT_FACT_SOURCE,
    MAX_ATTRIBUTION_WEIGHT,
    OVERLAP_POLICY_FORBID,
    Activity,
    Attribution,
    ContextFact,
    Entity,
    EntityAttributionShare,
    Facet,
    FacetValue,
    Interval,
    IntervalSlice,
    format_utc_iso,
)

# region Constants
ACTIVITY_KIND_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_ACTIVITY_KINDS)
)
CAPTURE_MODE_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_CAPTURE_MODES)
)
OVERLAP_POLICY_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_OVERLAP_POLICIES)
)
FACET_VALUE_TYPE_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (val, val) for val in sorted(ALLOWED_FACET_VALUE_TYPES)
)
# endregion


# region Serializers
@extend_schema_field(
    {
        "oneOf": [
            {"type": "string"},
            {"type": "integer"},
            {"type": "number"},
            {"type": "boolean"},
        ]
    }
)
class FacetScalarField(serializers.Field):  # type: ignore[misc]
    """Typed OpenAPI 3.1 scalar field accepting `str | int | float | bool` `FacetValue`s."""

    def to_representation(self, value: FacetValue) -> FacetValue:
        """Return scalar `FacetValue` unchanged."""
        return value

    def to_internal_value(self, data: Any) -> FacetValue:
        """Accept `bool`, `int`, `float`, or `str` while rejecting nested containers."""
        if isinstance(data, (bool, int, float, str)):
            return data
        raise serializers.ValidationError("Facet value must be string, integer, number, or bool.")


class ActivitySerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `Activity` transport schema for `pinia-orm`."""

    id = serializers.CharField(read_only=True)
    name = serializers.CharField()
    kind = serializers.ChoiceField(choices=ACTIVITY_KIND_CHOICES, default=ACTIVITY_KIND_TARGETED)
    external_ref = serializers.CharField(required=False, allow_null=True, default=None)
    rollup_target_ref = serializers.CharField(required=False, allow_null=True, default=None)
    source = serializers.CharField(required=False, default=DEFAULT_ACTIVITY_SOURCE)
    archived = serializers.BooleanField(required=False, default=False)
    tags = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class ActivityUpdateSerializer(serializers.Serializer):  # type: ignore[misc]
    """Partial update payload for `PATCH /api/v1/activities/{activity_id}/`."""

    name = serializers.CharField(required=False)
    external_ref = serializers.CharField(required=False, allow_null=True)
    rollup_target_ref = serializers.CharField(required=False, allow_null=True)
    archived = serializers.BooleanField(required=False)
    tags = serializers.ListField(child=serializers.CharField(), required=False)


class IntervalSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `Interval` transport schema for `pinia-orm`."""

    id = serializers.CharField(read_only=True)
    started_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField(required=False, allow_null=True)
    duration_seconds = serializers.FloatField(read_only=True, allow_null=True)
    is_open = serializers.BooleanField(read_only=True)
    is_allocated = serializers.BooleanField(read_only=True)
    activity_id = serializers.CharField(required=False, allow_null=True)
    capture_mode = serializers.ChoiceField(choices=CAPTURE_MODE_CHOICES)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    tags = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class IntervalCreateSerializer(serializers.Serializer):  # type: ignore[misc]
    """Payload for recording a closed structured or chaotic `Interval`."""

    capture_mode = serializers.ChoiceField(
        choices=CAPTURE_MODE_CHOICES,
        default=CAPTURE_MODE_STRUCTURED,
    )
    activity_id = serializers.CharField(required=False, allow_null=True, default=None)
    started_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField()
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    tags = serializers.ListField(child=serializers.CharField(), required=False, default=list)


class IntervalClockInSerializer(serializers.Serializer):  # type: ignore[misc]
    """Payload for `POST /api/v1/intervals/clock-in/`."""

    activity_id = serializers.CharField()
    started_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    tags = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    overlap_policy = serializers.ChoiceField(
        choices=OVERLAP_POLICY_CHOICES,
        default=OVERLAP_POLICY_FORBID,
    )


class IntervalClockOutSerializer(serializers.Serializer):  # type: ignore[misc]
    """Payload for `POST /api/v1/intervals/clock-out/`."""

    interval_id = serializers.CharField(required=False, allow_null=True, default=None)
    ended_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    tags = serializers.ListField(child=serializers.CharField(), required=False)


class IntervalBindSerializer(serializers.Serializer):  # type: ignore[misc]
    """Payload for `POST /api/v1/intervals/{interval_id}/bind/`."""

    activity_id = serializers.CharField()
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    allow_rebind = serializers.BooleanField(required=False, default=False)


class IntervalSliceInputSerializer(serializers.Serializer):  # type: ignore[misc]
    """Single contiguous slice item inside `IntervalSplitSerializer`."""

    activity_id = serializers.CharField(required=False, allow_null=True, default=None)
    duration_seconds = serializers.FloatField(required=False, allow_null=True, default=None)
    ended_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    tags = serializers.ListField(child=serializers.CharField(), required=False)


class IntervalSplitSerializer(serializers.Serializer):  # type: ignore[misc]
    """Payload for `POST /api/v1/intervals/{interval_id}/split/`."""

    slices = IntervalSliceInputSerializer(many=True)


class EntitySerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `Entity` transport schema for `pinia-orm`."""

    id = serializers.CharField(read_only=True)
    kind = serializers.CharField()
    key = serializers.CharField()
    slug = serializers.CharField(read_only=True)
    name = serializers.CharField()
    metadata = serializers.DictField(required=False, default=dict)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class AttributionSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `Attribution` edge transport schema for `pinia-orm`."""

    id = serializers.CharField(read_only=True)
    target_entity_id = serializers.CharField()
    weight = serializers.FloatField(default=MAX_ATTRIBUTION_WEIGHT)
    source_activity_id = serializers.CharField(required=False, allow_null=True, default=None)
    source_entity_id = serializers.CharField(required=False, allow_null=True, default=None)
    enforce_weight_sum = serializers.BooleanField(write_only=True, required=False, default=False)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class EntityAttributionShareSerializer(serializers.Serializer):  # type: ignore[misc]
    """Aggregated `EntityAttributionShare` response schema."""

    entity = EntitySerializer(read_only=True)
    attributed_seconds = serializers.FloatField(read_only=True)
    share_of_kind = serializers.FloatField(read_only=True)


class FacetSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `Facet` schema transport definition for `pinia-orm`."""

    key = serializers.CharField()
    value_type = serializers.ChoiceField(choices=FACET_VALUE_TYPE_CHOICES)
    allowed_values = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    description = serializers.CharField(required=False, allow_blank=True, default="")
    builtin = serializers.BooleanField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)


class ContextFactSerializer(serializers.Serializer):  # type: ignore[misc]
    """Canonical `ContextFact` transport schema for `pinia-orm`."""

    id = serializers.CharField(read_only=True)
    facet_key = serializers.CharField()
    value = FacetScalarField()
    scope_date = serializers.CharField(required=False, allow_null=True, default=None)
    interval_id = serializers.CharField(required=False, allow_null=True, default=None)
    source = serializers.CharField(required=False, default=DEFAULT_CONTEXT_FACT_SOURCE)
    updated_at = serializers.DateTimeField(read_only=True)


# endregion


# region Rendering
def activity_to_dict(activity: Activity) -> dict[str, object]:
    """Convert an `Activity` domain dataclass into a JSON-ready dictionary."""
    return {
        "id": activity.id,
        "name": activity.name,
        "kind": activity.kind,
        "external_ref": activity.external_ref,
        "rollup_target_ref": activity.rollup_target_ref,
        "source": activity.source,
        "archived": activity.archived,
        "tags": list(activity.tags),
        "created_at": format_utc_iso(activity.created_at),
        "updated_at": format_utc_iso(activity.updated_at),
    }


def interval_to_dict(interval: Interval) -> dict[str, object]:
    """Convert an `Interval` domain dataclass into a JSON-ready dictionary."""
    return {
        "id": interval.id,
        "started_at": format_utc_iso(interval.started_at),
        "ended_at": None if interval.ended_at is None else format_utc_iso(interval.ended_at),
        "duration_seconds": interval.duration_seconds,
        "is_open": interval.is_open,
        "is_allocated": interval.is_allocated,
        "activity_id": interval.activity_id,
        "capture_mode": interval.capture_mode,
        "notes": interval.notes,
        "tags": list(interval.tags),
        "created_at": format_utc_iso(interval.created_at),
        "updated_at": format_utc_iso(interval.updated_at),
    }


def entity_to_dict(entity: Entity) -> dict[str, object]:
    """Convert an `Entity` domain dataclass into a JSON-ready dictionary."""
    return {
        "id": entity.id,
        "kind": entity.kind,
        "key": entity.key,
        "slug": entity.slug,
        "name": entity.name,
        "metadata": dict(entity.metadata),
        "created_at": format_utc_iso(entity.created_at),
        "updated_at": format_utc_iso(entity.updated_at),
    }


def attribution_to_dict(attr: Attribution) -> dict[str, object]:
    """Convert an `Attribution` domain dataclass into a JSON-ready dictionary."""
    return {
        "id": attr.id,
        "target_entity_id": attr.target_entity_id,
        "weight": attr.weight,
        "source_activity_id": attr.source_activity_id,
        "source_entity_id": attr.source_entity_id,
        "created_at": format_utc_iso(attr.created_at),
        "updated_at": format_utc_iso(attr.updated_at),
    }


def attribution_share_to_dict(share: EntityAttributionShare) -> dict[str, object]:
    """Convert an `EntityAttributionShare` into a JSON-ready dictionary."""
    return {
        "entity": entity_to_dict(share.entity),
        "attributed_seconds": share.attributed_seconds,
        "share_of_kind": share.share_of_kind,
    }


def facet_to_dict(facet: Facet) -> dict[str, object]:
    """Convert a `Facet` domain dataclass into a JSON-ready dictionary."""
    return {
        "key": facet.key,
        "value_type": facet.value_type,
        "allowed_values": list(facet.allowed_values),
        "description": facet.description,
        "builtin": facet.builtin,
        "created_at": format_utc_iso(facet.created_at),
    }


def context_fact_to_dict(fact: ContextFact) -> dict[str, object]:
    """Convert a `ContextFact` domain dataclass into a JSON-ready dictionary."""
    return {
        "id": fact.id,
        "facet_key": fact.facet_key,
        "value": fact.value,
        "scope_date": fact.scope_date,
        "interval_id": fact.interval_id,
        "source": fact.source,
        "updated_at": format_utc_iso(fact.updated_at),
    }


def build_interval_slice(entry: dict[str, Any]) -> IntervalSlice:
    """Construct an `IntervalSlice` from a validated split-slice dictionary."""
    raw_dur = entry.get("duration_seconds")
    duration = None if raw_dur is None else timedelta(seconds=float(raw_dur))
    raw_tags = entry.get("tags")
    tags = None if raw_tags is None else tuple(str(t) for t in raw_tags)
    return IntervalSlice(
        activity_id=entry.get("activity_id"),
        duration=duration,
        ended_at=entry.get("ended_at"),
        notes=entry.get("notes"),
        tags=tags,
    )


# endregion
