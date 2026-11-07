"""Dynamic dual-mode Django + DRF + `drf-spectacular` settings module."""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Mapping
from typing import Any, Final

import django
from django.apps import apps

from worktrack.agenda.models import ALLOWED_AGENDA_HORIZONS, ALLOWED_AGENDA_STATUSES
from worktrack.config import ConfigurationError, WorktrackSettings
from worktrack.core.models import (
    ALLOWED_ACTIVITY_KINDS,
    ALLOWED_CAPTURE_MODES,
    ALLOWED_FACET_VALUE_TYPES,
    ALLOWED_OVERLAP_POLICIES,
)
from worktrack.outbox.models import (
    ALLOWED_CHANGE_KINDS,
    ALLOWED_EGRESS_OUTCOMES,
    ALLOWED_FLUSH_CADENCES,
    ALLOWED_FLUSH_TRIGGERS,
    ALLOWED_GROUPING_WINDOWS,
    ALLOWED_OUTBOX_STATUSES,
    ALLOWED_ROUNDING_MODES,
)
from worktrack.secrets.exceptions import SecretError
from worktrack.secrets.providers import SecretResolver
from worktrack.web.database import build_databases_config

# region Constants
DJANGO_SETTINGS_MODULE_NAME: Final = "worktrack.web.settings"
HUB_MODE: Final = "hub"
DEFAULT_SECRET_KEY_BYTES: Final = 48
OPENAPI_OAS_VERSION: Final = "3.1.0"
WORKTRACK_API_VERSION: Final = "0.1.0"

ENV_DJANGO_SECRET_KEY: Final = "WORKTRACK_DJANGO_SECRET_KEY"
ENV_DJANGO_SECRET_KEY_URI: Final = "WORKTRACK_DJANGO_SECRET_KEY_URI"
ENV_ALLOWED_HOSTS: Final = "WORKTRACK_ALLOWED_HOSTS"
ENV_CORS_ALLOWED_ORIGINS: Final = "WORKTRACK_CORS_ALLOWED_ORIGINS"
ENV_CSRF_TRUSTED_ORIGINS: Final = "WORKTRACK_CSRF_TRUSTED_ORIGINS"
ENV_DJANGO_DEBUG: Final = "WORKTRACK_DJANGO_DEBUG"

TRUTHY_FLAGS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

DEFAULT_NODE_ALLOWED_HOSTS: Final[tuple[str, ...]] = (
    "127.0.0.1",
    "localhost",
    "[::1]",
    "testserver",
)
DEFAULT_HUB_ALLOWED_HOSTS: Final[tuple[str, ...]] = (
    "127.0.0.1",
    "localhost",
    "[::1]",
)
DEFAULT_LOOPBACK_ORIGINS: Final[tuple[str, ...]] = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
)

INSTALLED_DJANGO_APPS: Final[tuple[str, ...]] = (
    "rest_framework",
    "drf_spectacular",
    "worktrack.web.apps.WorktrackWebConfig",
)

DJANGO_MIDDLEWARE: Final[tuple[str, ...]] = (
    "django.middleware.security.SecurityMiddleware",
    "worktrack.web.middleware.FritzTraceAndMetricsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
)

DEFAULT_REST_FRAMEWORK_SETTINGS: Final[dict[str, Any]] = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "UNAUTHENTICATED_USER": None,
    "EXCEPTION_HANDLER": "worktrack.web.services.worktrack_exception_handler",
}

DEFAULT_SPECTACULAR_SETTINGS: Final[dict[str, Any]] = {
    "OAS_VERSION": OPENAPI_OAS_VERSION,
    "TITLE": "Worktrack API",
    "DESCRIPTION": (
        "Anti-surveillance personal time ledger, horizon planner, "
        "previewable Outbox gate, and stateless OAuth PKCE broker."
    ),
    "VERSION": WORKTRACK_API_VERSION,
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "ActivityKindEnum": sorted(ALLOWED_ACTIVITY_KINDS),
        "CaptureModeEnum": sorted(ALLOWED_CAPTURE_MODES),
        "OverlapPolicyEnum": sorted(ALLOWED_OVERLAP_POLICIES),
        "FacetValueTypeEnum": sorted(ALLOWED_FACET_VALUE_TYPES),
        "AgendaHorizonEnum": sorted(ALLOWED_AGENDA_HORIZONS),
        "AgendaStatusEnum": sorted(ALLOWED_AGENDA_STATUSES),
        "OutboxStatusEnum": sorted(ALLOWED_OUTBOX_STATUSES),
        "FlushCadenceEnum": sorted(ALLOWED_FLUSH_CADENCES),
        "FlushTriggerEnum": sorted(ALLOWED_FLUSH_TRIGGERS),
        "GroupingWindowEnum": sorted(ALLOWED_GROUPING_WINDOWS),
        "RoundingModeEnum": sorted(ALLOWED_ROUNDING_MODES),
        "PreviewChangeKindEnum": sorted(ALLOWED_CHANGE_KINDS),
        "EgressOutcomeEnum": sorted(ALLOWED_EGRESS_OUTCOMES),
    },
}
# endregion


# region Resolution
def resolve_django_secret_key(
    settings: WorktrackSettings,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve Django `SECRET_KEY` from URI/env in `hub` mode or ephemeral random in `node` mode."""
    env = os.environ if environ is None else environ
    secret_uri = env.get(ENV_DJANGO_SECRET_KEY_URI, "").strip()
    if secret_uri:
        try:
            return SecretResolver.from_env(environ=env).resolve(secret_uri).expose()
        except SecretError as exc:
            raise ConfigurationError(
                f"Failed to resolve {ENV_DJANGO_SECRET_KEY_URI}: {exc}"
            ) from exc

    explicit = env.get(ENV_DJANGO_SECRET_KEY, "").strip()
    if explicit:
        return explicit

    if settings.mode == HUB_MODE:
        raise ConfigurationError(
            f"{ENV_DJANGO_SECRET_KEY} or {ENV_DJANGO_SECRET_KEY_URI} is required in hub mode."
        )
    return secrets.token_urlsafe(DEFAULT_SECRET_KEY_BYTES)


def _split_csv_env(
    env: Mapping[str, str],
    key: str,
    defaults: tuple[str, ...],
) -> list[str]:
    """Split a comma-delimited environment variable or fall back to `defaults`."""
    raw = env.get(key, "").strip()
    if not raw:
        return list(defaults)
    items = [part.strip() for part in raw.split(",") if part.strip()]
    return items if items else list(defaults)


def build_django_settings_dict(
    *,
    environ: Mapping[str, str] | None = None,
    worktrack_settings: WorktrackSettings | None = None,
) -> dict[str, Any]:
    """Assemble a complete Django settings dictionary from `WorktrackSettings` and `environ`."""
    env = os.environ if environ is None else environ
    wt_settings = (
        WorktrackSettings.from_env(environ=env)
        if worktrack_settings is None
        else worktrack_settings
    )
    secret_key = resolve_django_secret_key(wt_settings, environ=env)
    host_defaults = (
        DEFAULT_HUB_ALLOWED_HOSTS if wt_settings.mode == HUB_MODE else DEFAULT_NODE_ALLOWED_HOSTS
    )
    raw_debug = env.get(ENV_DJANGO_DEBUG, "").strip().lower()
    debug_enabled = raw_debug in TRUTHY_FLAGS if raw_debug else False
    if debug_enabled and wt_settings.mode == HUB_MODE:
        raise ConfigurationError(f"{ENV_DJANGO_DEBUG} cannot be enabled when mode='hub'.")

    cors_origins = _split_csv_env(env, ENV_CORS_ALLOWED_ORIGINS, DEFAULT_LOOPBACK_ORIGINS)
    csrf_origins = _split_csv_env(env, ENV_CSRF_TRUSTED_ORIGINS, DEFAULT_LOOPBACK_ORIGINS)

    return {
        "SECRET_KEY": secret_key,
        "DEBUG": debug_enabled,
        "ALLOWED_HOSTS": _split_csv_env(env, ENV_ALLOWED_HOSTS, host_defaults),
        "INSTALLED_APPS": list(INSTALLED_DJANGO_APPS),
        "MIDDLEWARE": list(DJANGO_MIDDLEWARE),
        "ROOT_URLCONF": "worktrack.web.urls",
        "WSGI_APPLICATION": "worktrack.web.wsgi.application",
        "ASGI_APPLICATION": "worktrack.web.asgi.application",
        "DATABASES": build_databases_config(wt_settings, environ=env),
        "LANGUAGE_CODE": "en-us",
        "TIME_ZONE": "UTC",
        "USE_I18N": False,
        "USE_TZ": True,
        "DEFAULT_AUTO_FIELD": "django.db.models.BigAutoField",
        "CORS_ALLOWED_ORIGINS": cors_origins,
        "CSRF_TRUSTED_ORIGINS": csrf_origins,
        "WORKTRACK_SETTINGS": wt_settings,
        "REST_FRAMEWORK": dict(DEFAULT_REST_FRAMEWORK_SETTINGS),
        "SPECTACULAR_SETTINGS": dict(DEFAULT_SPECTACULAR_SETTINGS),
    }


def ensure_django_configured(
    *,
    environ: Mapping[str, str] | None = None,
    worktrack_settings: WorktrackSettings | None = None,
) -> None:
    """Set `DJANGO_SETTINGS_MODULE` if unset and run `django.setup()` idempotently."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", DJANGO_SETTINGS_MODULE_NAME)
    if environ is not None or worktrack_settings is not None:
        _populate_module_settings(environ=environ, worktrack_settings=worktrack_settings)
    if not apps.ready and not apps.loading:
        django.setup()


def _populate_module_settings(
    *,
    environ: Mapping[str, str] | None = None,
    worktrack_settings: WorktrackSettings | None = None,
) -> None:
    """Populate module globals when imported by Django's settings loader."""
    current_module = sys.modules[__name__]
    built = build_django_settings_dict(environ=environ, worktrack_settings=worktrack_settings)
    for setting_key, setting_value in built.items():
        setattr(current_module, setting_key, setting_value)


_populate_module_settings()
# endregion
