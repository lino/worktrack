"""`FritzTraceAndMetricsMiddleware` for W3C trace propagation, CORS, and HTTP telemetry."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

from django.conf import settings as django_settings
from django.http import HttpRequest, HttpResponse

from worktrack.telemetry import resolve_trace_id
from worktrack.web.services import WebServiceContainer, get_active_web_services, use_web_services

# region Constants
HEADER_FRITZ_TRACE_ID: Final = "X-Fritz-Trace-Id"
HEADER_LEGACY_FRITZ_TRACE: Final = "Fritz-Trace-Id"
HEADER_W3C_TRACEPARENT: Final = "traceparent"

EVENT_HTTP_REQUEST_COMPLETED: Final = "http.request.completed"
METRIC_HTTP_REQUESTS_TOTAL: Final = "worktrack_http_requests_total"
METRIC_HTTP_DURATION_SECONDS: Final = "worktrack_http_request_duration_seconds"

UNMATCHED_ROUTE_LABEL: Final = "unmatched"
UNKNOWN_METHOD_LABEL: Final = "OTHER"
ALLOWED_HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)

HTTP_STATUS_CLASS_DIVISOR: Final = 100
CORS_ALLOW_METHODS: Final = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
CORS_ALLOW_HEADERS: Final = "Accept, Authorization, Content-Type, X-Fritz-Trace-Id, traceparent"

ROUTE_LABEL_BY_URL_NAME: Final[dict[str, str]] = {
    "healthz": "healthz",
    "metrics": "metrics",
    "api_v1_schema": "api_v1_schema",
    "api_v1_activities_list": "api_v1_activities",
    "api_v1_activities_detail": "api_v1_activities",
    "api_v1_intervals_list": "api_v1_intervals",
    "api_v1_intervals_detail": "api_v1_intervals",
    "api_v1_intervals_clock_in": "api_v1_intervals",
    "api_v1_intervals_clock_out": "api_v1_intervals",
    "api_v1_intervals_bind": "api_v1_intervals",
    "api_v1_intervals_split": "api_v1_intervals",
    "api_v1_entities_list": "api_v1_entities",
    "api_v1_entities_detail": "api_v1_entities",
    "api_v1_attributions_list": "api_v1_attributions",
    "api_v1_attributions_detail": "api_v1_attributions",
    "api_v1_attributions_shares": "api_v1_attributions",
    "api_v1_facets_list": "api_v1_facets",
    "api_v1_facets_detail": "api_v1_facets",
    "api_v1_context_facts_list": "api_v1_context_facts",
    "api_v1_agenda_items_list": "api_v1_agenda_items",
    "api_v1_agenda_items_detail": "api_v1_agenda_items",
    "api_v1_focus_blocks_list": "api_v1_focus_blocks",
    "api_v1_focus_blocks_detail": "api_v1_focus_blocks",
    "api_v1_insights_now": "api_v1_insights",
    "api_v1_insights_today": "api_v1_insights",
    "api_v1_insights_week": "api_v1_insights",
    "api_v1_outbox_items_list": "api_v1_outbox",
    "api_v1_outbox_items_detail": "api_v1_outbox",
    "api_v1_outbox_preview": "api_v1_outbox",
    "api_v1_outbox_approve": "api_v1_outbox",
    "api_v1_outbox_reject": "api_v1_outbox",
    "api_v1_outbox_flush": "api_v1_outbox",
    "api_v1_egress_ledger_list": "api_v1_egress_ledger",
    "api_v1_oauth_exchange": "api_v1_oauth_exchange",
    "api_v1_oauth_refresh": "api_v1_oauth_refresh",
}
# endregion


# region Middleware
class FritzTraceAndMetricsMiddleware:
    """Propagate `FRITZ_TRACE_ID`, apply loopback CORS, and record bounded HTTP metrics."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Store downstream Django `get_response` callable."""
        self._get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Execute request with trace propagation, CORS headers, and Prometheus observation."""
        trace_id = extract_request_trace_id(request)
        request.fritz_trace_id = trace_id
        services = get_active_web_services()
        started_mono = time.perf_counter()

        origin = request.headers.get("Origin", "").strip()
        if request.method == "OPTIONS" and _is_allowed_cors_origin(origin):
            response = HttpResponse(status=204)
        else:
            with use_web_services(services):
                response = self._get_response(request)

        elapsed_seconds = max(0.0, time.perf_counter() - started_mono)
        response[HEADER_FRITZ_TRACE_ID] = trace_id
        _attach_cors_headers(response, origin)
        _record_http_telemetry(
            services,
            request,
            response,
            trace_id=trace_id,
            duration=elapsed_seconds,
        )
        return response


def extract_request_trace_id(request: HttpRequest) -> str:
    """Extract `X-Fritz-Trace-Id` or W3C `traceparent` from `request`, or mint a 32-hex ID."""
    explicit_header = (
        request.headers.get(HEADER_FRITZ_TRACE_ID, "").strip()
        or request.headers.get(HEADER_LEGACY_FRITZ_TRACE, "").strip()
    )
    traceparent = request.headers.get(HEADER_W3C_TRACEPARENT, "").strip()
    header_env: dict[str, str] = {}
    if explicit_header:
        header_env["FRITZ_TRACE_ID"] = explicit_header
    if traceparent:
        header_env["TRACEPARENT"] = traceparent
    return resolve_trace_id(environ=header_env if header_env else None)


def resolve_bounded_route_label(request: HttpRequest) -> str:
    """Map `request.resolver_match` onto a slash-free, bounded canonical route label."""
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", None)
    if isinstance(url_name, str) and url_name in ROUTE_LABEL_BY_URL_NAME:
        return ROUTE_LABEL_BY_URL_NAME[url_name]
    return UNMATCHED_ROUTE_LABEL


def _normalize_http_method(raw_method: str | None) -> str:
    """Normalize HTTP method verb to the bounded uppercase allowlist."""
    cleaned = (raw_method or "").strip().upper()
    return cleaned if cleaned in ALLOWED_HTTP_METHODS else UNKNOWN_METHOD_LABEL


def _format_status_class(status_code: int) -> str:
    """Bucket numeric HTTP status code into `'2xx'`, `'3xx'`, `'4xx'`, or `'5xx'`."""
    bucket = max(1, min(5, int(status_code) // HTTP_STATUS_CLASS_DIVISOR))
    return f"{bucket}xx"


def _is_allowed_cors_origin(origin: str) -> bool:
    """Return `True` when `origin` is listed in `django.conf.settings.CORS_ALLOWED_ORIGINS`."""
    if not origin:
        return False
    allowed: list[str] = list(getattr(django_settings, "CORS_ALLOWED_ORIGINS", ()))
    return origin in allowed


def _attach_cors_headers(response: HttpResponse, origin: str) -> None:
    """Attach loopback CORS headers when `origin` matches configured origins."""
    if not _is_allowed_cors_origin(origin):
        return
    response["Access-Control-Allow-Origin"] = origin
    response["Access-Control-Allow-Methods"] = CORS_ALLOW_METHODS
    response["Access-Control-Allow-Headers"] = CORS_ALLOW_HEADERS
    response["Access-Control-Expose-Headers"] = HEADER_FRITZ_TRACE_ID
    response["Vary"] = "Origin"


def _record_http_telemetry(
    services: WebServiceContainer,
    request: HttpRequest,
    response: HttpResponse,
    *,
    trace_id: str,
    duration: float,
) -> None:
    """Record bounded Prometheus HTTP metrics and redacted JSONL request trace event."""
    method_lbl = _normalize_http_method(request.method)
    route_lbl = resolve_bounded_route_label(request)
    status_class = _format_status_class(response.status_code)
    services.telemetry.increment_counter(
        METRIC_HTTP_REQUESTS_TOTAL,
        {"method": method_lbl, "route": route_lbl, "status_class": status_class},
    )
    services.telemetry.observe_histogram(
        METRIC_HTTP_DURATION_SECONDS,
        {"method": method_lbl, "route": route_lbl},
        duration,
    )
    services.event_logger.emit(
        EVENT_HTTP_REQUEST_COMPLETED,
        trace_id=trace_id,
        environ=services.environ,
        method=method_lbl,
        route=route_lbl,
        status_code=int(response.status_code),
        status_class=status_class,
        duration_seconds=round(duration, 6),
    )


# endregion
