"""DRF `APIView` endpoints for `Entity`, `Attribution`, `Facet`, and `ContextFact` graphs."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from worktrack.web.serializers_core import (
    AttributionSerializer,
    ContextFactSerializer,
    EntityAttributionShareSerializer,
    EntitySerializer,
    FacetSerializer,
    attribution_share_to_dict,
    attribution_to_dict,
    context_fact_to_dict,
    entity_to_dict,
    facet_to_dict,
)
from worktrack.web.services import get_active_web_services
from worktrack.web.views_core import optional_str_param, parse_bool_param, parse_dt_param


# region Views
class EntityListCreateView(APIView):  # type: ignore[misc]
    """List or create non-strict `Entities` (`/api/v1/entities/`)."""

    @extend_schema(
        tags=["Entities"],
        parameters=[OpenApiParameter("kind", OpenApiTypes.STR, required=False)],
        responses={200: EntitySerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """List `Entities` optionally filtered by `kind`."""
        with get_active_web_services().open_ledger() as ledger:
            entities = ledger.list_entities(kind=optional_str_param(request, "kind"))
        return Response([entity_to_dict(item) for item in entities])

    @extend_schema(
        tags=["Entities"],
        request=EntitySerializer,
        responses={201: EntitySerializer},
    )
    def post(self, request: Request) -> Response:
        """Create a new `Entity` node."""
        serializer = EntitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with get_active_web_services().open_ledger() as ledger:
            entity = ledger.create_entity(
                str(data["kind"]),
                str(data["key"]),
                str(data["name"]),
                metadata=dict(data.get("metadata") or {}),
            )
        return Response(entity_to_dict(entity), status=status.HTTP_201_CREATED)


class EntityDetailView(APIView):  # type: ignore[misc]
    """Retrieve a single `Entity` (`/api/v1/entities/{entity_id}/`)."""

    @extend_schema(tags=["Entities"], responses={200: EntitySerializer})
    def get(self, _request: Request, entity_id: str) -> Response:
        """Fetch an `Entity` by identifier."""
        with get_active_web_services().open_ledger() as ledger:
            entity = ledger.get_entity(entity_id)
        return Response(entity_to_dict(entity))


class AttributionListCreateView(APIView):  # type: ignore[misc]
    """List or upsert weighted DAG `Attribution` edges (`/api/v1/attributions/`)."""

    @extend_schema(
        tags=["Attributions"],
        parameters=[
            OpenApiParameter("source_activity_id", OpenApiTypes.STR, required=False),
            OpenApiParameter("source_entity_id", OpenApiTypes.STR, required=False),
            OpenApiParameter("target_entity_id", OpenApiTypes.STR, required=False),
        ],
        responses={200: AttributionSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """List `Attribution` edges matching optional source or target filters."""
        with get_active_web_services().open_ledger() as ledger:
            edges = ledger.list_attributions(
                source_activity_id=optional_str_param(request, "source_activity_id"),
                source_entity_id=optional_str_param(request, "source_entity_id"),
                target_entity_id=optional_str_param(request, "target_entity_id"),
            )
        return Response([attribution_to_dict(edge) for edge in edges])

    @extend_schema(
        tags=["Attributions"],
        request=AttributionSerializer,
        responses={201: AttributionSerializer},
    )
    def post(self, request: Request) -> Response:
        """Upsert a cycle-checked weighted `Attribution` edge."""
        serializer = AttributionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with get_active_web_services().open_ledger() as ledger:
            edge = ledger.set_attribution(
                target_entity_id=str(data["target_entity_id"]),
                weight=float(data["weight"]),
                source_activity_id=data.get("source_activity_id"),
                source_entity_id=data.get("source_entity_id"),
                enforce_weight_sum=bool(data.get("enforce_weight_sum", False)),
            )
        return Response(attribution_to_dict(edge), status=status.HTTP_201_CREATED)


class AttributionDetailView(APIView):  # type: ignore[misc]
    """Remove an `Attribution` edge (`/api/v1/attributions/{attribution_id}/`)."""

    @extend_schema(tags=["Attributions"], responses={204: None})
    def delete(self, _request: Request, attribution_id: str) -> Response:
        """Delete an `Attribution` edge by identifier."""
        with get_active_web_services().open_ledger() as ledger:
            ledger.remove_attribution(attribution_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AttributionSharesView(APIView):  # type: ignore[misc]
    """Query aggregated `EntityAttributionShare` rollups (`/api/v1/attributions/shares/`)."""

    @extend_schema(
        tags=["Attributions"],
        parameters=[
            OpenApiParameter("since", OpenApiTypes.DATETIME, required=False),
            OpenApiParameter("until", OpenApiTypes.DATETIME, required=False),
            OpenApiParameter("target_kind", OpenApiTypes.STR, required=False),
            OpenApiParameter("normalize", OpenApiTypes.BOOL, required=False),
        ],
        responses={200: EntityAttributionShareSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """Compute transitive `EntityAttributionShare` totals across the DAG."""
        since = parse_dt_param(request, "since")
        until = parse_dt_param(request, "until")
        normalize = parse_bool_param(request, "normalize", default=False)
        with get_active_web_services().open_ledger() as ledger:
            shares = ledger.query_entity_attribution(
                since=since,
                until=until,
                target_kind=optional_str_param(request, "target_kind"),
                normalize=normalize,
            )
        return Response([attribution_share_to_dict(share) for share in shares])


class FacetListCreateView(APIView):  # type: ignore[misc]
    """List or register typed `Facet` schemas (`/api/v1/facets/`)."""

    @extend_schema(tags=["Facets"], responses={200: FacetSerializer(many=True)})
    def get(self, _request: Request) -> Response:
        """List all registered `Facet` schemas."""
        with get_active_web_services().open_ledger() as ledger:
            facets = ledger.list_facets()
        return Response([facet_to_dict(item) for item in facets])

    @extend_schema(
        tags=["Facets"],
        request=FacetSerializer,
        responses={201: FacetSerializer},
    )
    def post(self, request: Request) -> Response:
        """Register a custom typed `Facet` schema."""
        serializer = FacetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with get_active_web_services().open_ledger() as ledger:
            facet = ledger.register_facet(
                str(data["key"]),
                str(data["value_type"]),
                allowed_values=list(data.get("allowed_values") or ()),
                description=str(data.get("description") or ""),
            )
        return Response(facet_to_dict(facet), status=status.HTTP_201_CREATED)


class FacetDetailView(APIView):  # type: ignore[misc]
    """Retrieve a registered `Facet` schema by key (`/api/v1/facets/{key}/`)."""

    @extend_schema(tags=["Facets"], responses={200: FacetSerializer})
    def get(self, _request: Request, key: str) -> Response:
        """Fetch a `Facet` schema by `key`."""
        with get_active_web_services().open_ledger() as ledger:
            facet = ledger.get_facet(key)
        return Response(facet_to_dict(facet))


class ContextFactListCreateView(APIView):  # type: ignore[misc]
    """Query or assert `ContextFacts` (`/api/v1/context-facts/`)."""

    @extend_schema(
        tags=["Facets"],
        parameters=[
            OpenApiParameter("facet_key", OpenApiTypes.STR, required=False),
            OpenApiParameter("since_date", OpenApiTypes.STR, required=False),
            OpenApiParameter("until_date", OpenApiTypes.STR, required=False),
            OpenApiParameter("scope_date", OpenApiTypes.STR, required=False),
            OpenApiParameter("interval_id", OpenApiTypes.STR, required=False),
        ],
        responses={200: ContextFactSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        """List `ContextFacts` filtered by optional facet key, date window, or interval."""
        with get_active_web_services().open_ledger() as ledger:
            facts = ledger.list_context_facts(
                facet_key=optional_str_param(request, "facet_key"),
                since_date=optional_str_param(request, "since_date"),
                until_date=optional_str_param(request, "until_date"),
                scope_date=optional_str_param(request, "scope_date"),
                interval_id=optional_str_param(request, "interval_id"),
            )
        return Response([context_fact_to_dict(fact) for fact in facts])

    @extend_schema(
        tags=["Facets"],
        request=ContextFactSerializer,
        responses={201: ContextFactSerializer},
    )
    def post(self, request: Request) -> Response:
        """Assert or upsert a typed `ContextFact` on a date or `Interval`."""
        serializer = ContextFactSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with get_active_web_services().open_ledger() as ledger:
            fact = ledger.assert_context_fact(
                str(data["facet_key"]),
                data["value"],
                scope_date=data.get("scope_date"),
                interval_id=data.get("interval_id"),
                source=str(data["source"]),
            )
        return Response(context_fact_to_dict(fact), status=status.HTTP_201_CREATED)


# endregion
