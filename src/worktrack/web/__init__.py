"""Worktrack dual-mode Django + DRF + `drf-spectacular` API and Stateless OAuth Broker."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from worktrack.web.database import (
    DJANGO_POSTGRES_ENGINE,
    DJANGO_SQLITE_ENGINE,
    ENV_DATABASE_URL,
    ENV_WORKTRACK_DATABASE_URL,
    build_databases_config,
    parse_database_url,
)

if TYPE_CHECKING:
    from worktrack.web.asgi import get_asgi_application
    from worktrack.web.middleware import (
        HEADER_FRITZ_TRACE_ID,
        ROUTE_LABEL_BY_URL_NAME,
        FritzTraceAndMetricsMiddleware,
        extract_request_trace_id,
        resolve_bounded_route_label,
    )
    from worktrack.web.schema_export import (
        DEFAULT_SCHEMA_OUTPUT_PATH,
        ENV_OPENAPI_SCHEMA_PATH,
        export_openapi_schema,
        generate_openapi_schema_dict,
        resolve_schema_output_path,
        validate_openapi_schema_zero_warnings,
    )
    from worktrack.web.services import (
        ENV_OAUTH_PROVIDERS_JSON,
        WebServiceContainer,
        get_active_web_services,
        load_oauth_provider_configs,
        reset_default_web_services,
        use_web_services,
        worktrack_exception_handler,
    )
    from worktrack.web.settings import (
        DJANGO_SETTINGS_MODULE_NAME,
        ENV_ALLOWED_HOSTS,
        ENV_CORS_ALLOWED_ORIGINS,
        ENV_CSRF_TRUSTED_ORIGINS,
        ENV_DJANGO_DEBUG,
        ENV_DJANGO_SECRET_KEY,
        ENV_DJANGO_SECRET_KEY_URI,
        OPENAPI_OAS_VERSION,
        build_django_settings_dict,
        ensure_django_configured,
        resolve_django_secret_key,
    )
    from worktrack.web.wsgi import get_wsgi_application

_LAZY_SYMBOL_MODULES: dict[str, str] = {
    "get_asgi_application": "worktrack.web.asgi",
    "get_wsgi_application": "worktrack.web.wsgi",
    "HEADER_FRITZ_TRACE_ID": "worktrack.web.middleware",
    "ROUTE_LABEL_BY_URL_NAME": "worktrack.web.middleware",
    "FritzTraceAndMetricsMiddleware": "worktrack.web.middleware",
    "extract_request_trace_id": "worktrack.web.middleware",
    "resolve_bounded_route_label": "worktrack.web.middleware",
    "DEFAULT_SCHEMA_OUTPUT_PATH": "worktrack.web.schema_export",
    "ENV_OPENAPI_SCHEMA_PATH": "worktrack.web.schema_export",
    "export_openapi_schema": "worktrack.web.schema_export",
    "generate_openapi_schema_dict": "worktrack.web.schema_export",
    "resolve_schema_output_path": "worktrack.web.schema_export",
    "validate_openapi_schema_zero_warnings": "worktrack.web.schema_export",
    "ENV_OAUTH_PROVIDERS_JSON": "worktrack.web.services",
    "WebServiceContainer": "worktrack.web.services",
    "get_active_web_services": "worktrack.web.services",
    "load_oauth_provider_configs": "worktrack.web.services",
    "reset_default_web_services": "worktrack.web.services",
    "use_web_services": "worktrack.web.services",
    "worktrack_exception_handler": "worktrack.web.services",
    "DJANGO_SETTINGS_MODULE_NAME": "worktrack.web.settings",
    "ENV_ALLOWED_HOSTS": "worktrack.web.settings",
    "ENV_CORS_ALLOWED_ORIGINS": "worktrack.web.settings",
    "ENV_CSRF_TRUSTED_ORIGINS": "worktrack.web.settings",
    "ENV_DJANGO_DEBUG": "worktrack.web.settings",
    "ENV_DJANGO_SECRET_KEY": "worktrack.web.settings",
    "ENV_DJANGO_SECRET_KEY_URI": "worktrack.web.settings",
    "OPENAPI_OAS_VERSION": "worktrack.web.settings",
    "build_django_settings_dict": "worktrack.web.settings",
    "ensure_django_configured": "worktrack.web.settings",
    "resolve_django_secret_key": "worktrack.web.settings",
}


def __getattr__(name: str) -> Any:
    """Lazily resolve `worktrack.web` public symbols without eager Django setup side effects."""
    module_path = _LAZY_SYMBOL_MODULES.get(name)
    if module_path is not None:
        mod = import_module(module_path)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_SCHEMA_OUTPUT_PATH",
    "DJANGO_POSTGRES_ENGINE",
    "DJANGO_SETTINGS_MODULE_NAME",
    "DJANGO_SQLITE_ENGINE",
    "ENV_ALLOWED_HOSTS",
    "ENV_CORS_ALLOWED_ORIGINS",
    "ENV_CSRF_TRUSTED_ORIGINS",
    "ENV_DATABASE_URL",
    "ENV_DJANGO_DEBUG",
    "ENV_DJANGO_SECRET_KEY",
    "ENV_DJANGO_SECRET_KEY_URI",
    "ENV_OAUTH_PROVIDERS_JSON",
    "ENV_OPENAPI_SCHEMA_PATH",
    "ENV_WORKTRACK_DATABASE_URL",
    "HEADER_FRITZ_TRACE_ID",
    "OPENAPI_OAS_VERSION",
    "ROUTE_LABEL_BY_URL_NAME",
    "FritzTraceAndMetricsMiddleware",
    "WebServiceContainer",
    "build_databases_config",
    "build_django_settings_dict",
    "ensure_django_configured",
    "export_openapi_schema",
    "extract_request_trace_id",
    "generate_openapi_schema_dict",
    "get_active_web_services",
    "get_asgi_application",
    "get_wsgi_application",
    "load_oauth_provider_configs",
    "parse_database_url",
    "reset_default_web_services",
    "resolve_bounded_route_label",
    "resolve_django_secret_key",
    "resolve_schema_output_path",
    "use_web_services",
    "validate_openapi_schema_zero_warnings",
    "worktrack_exception_handler",
]
