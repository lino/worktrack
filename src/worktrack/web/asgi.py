"""ASGI application entrypoint for `worktrack.web`."""

from __future__ import annotations

from django.core.asgi import get_asgi_application as _django_get_asgi_application
from django.core.handlers.asgi import ASGIHandler

from worktrack.web.settings import ensure_django_configured


def get_asgi_application() -> ASGIHandler:
    """Configure Django idempotently and return the Worktrack `ASGIHandler`."""
    ensure_django_configured()
    return _django_get_asgi_application()


application: ASGIHandler = get_asgi_application()
