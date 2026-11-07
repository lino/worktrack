"""Operational `/healthz` and Prometheus `/metrics` endpoints for `worktrack.web`."""

from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from prometheus_client import CONTENT_TYPE_LATEST
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from worktrack.config import APP_SLUG
from worktrack.web.serializers_outbox import HealthzResponseSerializer
from worktrack.web.services import get_active_web_services


# region Views
class HealthzView(APIView):  # type: ignore[misc]
    """Readiness probe returning active Worktrack deployment mode (`/healthz`)."""

    @extend_schema(tags=["Operations"], responses={200: HealthzResponseSerializer})
    def get(self, _request: Request) -> Response:
        """Return `{'status': 'ok', 'mode': ..., 'namespace': 'worktrack'}`."""
        services = get_active_web_services()
        return Response(
            {
                "status": "ok",
                "mode": services.settings.mode,
                "namespace": APP_SLUG,
            }
        )


class MetricsExpositionView(APIView):  # type: ignore[misc]
    """Prometheus text exposition endpoint backed by `TelemetryRecorder` (`/metrics`)."""

    @extend_schema(
        tags=["Operations"],
        responses={(200, "text/plain"): OpenApiTypes.STR},
    )
    def get(self, _request: Request) -> HttpResponse:
        """Render persisted Worktrack Prometheus metric families."""
        payload = get_active_web_services().telemetry.render_metrics()
        return HttpResponse(payload, content_type=CONTENT_TYPE_LATEST)


# endregion
