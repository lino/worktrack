"""Dual-mode `DATABASE_URL` parser (`postgres://`, `sqlite:///`) and SQLite Node fallback."""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from typing import Any, Final

from worktrack.config import ConfigurationError, WorktrackSettings

# region Constants
ENV_DATABASE_URL: Final = "DATABASE_URL"
ENV_WORKTRACK_DATABASE_URL: Final = "WORKTRACK_DATABASE_URL"

POSTGRES_SCHEMES: Final[frozenset[str]] = frozenset({"postgres", "postgresql"})
SQLITE_SCHEME: Final = "sqlite"

DJANGO_POSTGRES_ENGINE: Final = "django.db.backends.postgresql"
DJANGO_SQLITE_ENGINE: Final = "django.db.backends.sqlite3"
SQLITE_MEMORY_TARGET: Final = ":memory:"
# endregion


# region Resolution
def parse_database_url(database_url: str) -> dict[str, Any]:
    """Parse a `postgres://`, `postgresql://`, or `sqlite:///` URL into a Django `DATABASES` dict.

    Args:
        database_url: Connection URL string (`postgresql://user:pw@host:5432/db` or `sqlite:///...`).

    Returns:
        Django `DATABASES['default']` configuration dictionary.

    Raises:
        ConfigurationError: If `database_url` is empty, malformed, or uses an unsupported scheme.
    """
    cleaned = database_url.strip()
    if not cleaned:
        raise ConfigurationError("DATABASE_URL must be a non-empty URL string.")
    parsed = urllib.parse.urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme in POSTGRES_SCHEMES:
        return _parse_postgres_url(parsed)
    if scheme == SQLITE_SCHEME:
        return _parse_sqlite_url(cleaned, parsed)
    raise ConfigurationError(
        f"Unsupported DATABASE_URL scheme '{scheme}'; expected postgresql:// or sqlite:///."
    )


def _parse_postgres_url(parsed: urllib.parse.SplitResult) -> dict[str, Any]:
    """Build a `django.db.backends.postgresql` config dictionary from a split URL."""
    host = (parsed.hostname or "").strip()
    db_name = urllib.parse.unquote(parsed.path.lstrip("/")).strip()
    if not host or not db_name:
        raise ConfigurationError(
            "PostgreSQL DATABASE_URL must include both a hostname and database name."
        )
    try:
        port_str = "" if parsed.port is None else str(parsed.port)
    except ValueError as exc:
        raise ConfigurationError("Invalid TCP port in PostgreSQL DATABASE_URL.") from exc

    user = "" if parsed.username is None else urllib.parse.unquote(parsed.username)
    password = "" if parsed.password is None else urllib.parse.unquote(parsed.password)
    options = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=False))
    config: dict[str, Any] = {
        "ENGINE": DJANGO_POSTGRES_ENGINE,
        "NAME": db_name,
        "USER": user,
        "PASSWORD": password,
        "HOST": host,
        "PORT": port_str,
    }
    if options:
        config["OPTIONS"] = options
    return config


def _parse_sqlite_url(raw_url: str, parsed: urllib.parse.SplitResult) -> dict[str, Any]:
    """Build a `django.db.backends.sqlite3` config dictionary from a `sqlite:///` URL."""
    if parsed.netloc:
        raise ConfigurationError("SQLite DATABASE_URL must use 'sqlite:///...' without netloc.")
    if not raw_url.startswith("sqlite:///"):
        raise ConfigurationError("SQLite DATABASE_URL must start with 'sqlite:///'.")
    raw_path = urllib.parse.unquote(parsed.path)
    if raw_path in ("/:memory:", SQLITE_MEMORY_TARGET):
        return {"ENGINE": DJANGO_SQLITE_ENGINE, "NAME": SQLITE_MEMORY_TARGET}
    if not raw_path or raw_path == "/":
        raise ConfigurationError("SQLite DATABASE_URL must specify a database file path.")
    normalized_path = "/" + raw_path.lstrip("/") if raw_path.startswith("//") else raw_path
    return {"ENGINE": DJANGO_SQLITE_ENGINE, "NAME": normalized_path}


def build_databases_config(
    settings: WorktrackSettings,
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build Django `DATABASES` mapping from `DATABASE_URL` or `WorktrackSettings.db_path`."""
    env = os.environ if environ is None else environ
    raw_url = (
        env.get(ENV_WORKTRACK_DATABASE_URL, "").strip() or env.get(ENV_DATABASE_URL, "").strip()
    )
    if raw_url:
        return {"default": parse_database_url(raw_url)}
    return {
        "default": {
            "ENGINE": DJANGO_SQLITE_ENGINE,
            "NAME": str(settings.db_path),
        }
    }


# endregion
