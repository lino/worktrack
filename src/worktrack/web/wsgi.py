"""WSGI application entrypoint for `worktrack.web`."""

from __future__ import annotations

from django.core.handlers.wsgi import WSGIHandler
from django.core.wsgi import get_wsgi_application as _django_get_wsgi_application

from worktrack.web.settings import ensure_django_configured


def get_wsgi_application() -> WSGIHandler:
    """Configure Django idempotently and return the Worktrack `WSGIHandler`."""
    ensure_django_configured()
    return _django_get_wsgi_application()


application: WSGIHandler = get_wsgi_application()
