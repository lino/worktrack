"""Behavioural tests for dual-mode Django settings, DATABASE_URL, telemetry, and WSGI/ASGI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from worktrack.config import ConfigurationError, WorktrackSettings
from worktrack.web import (
    WebServiceContainer,
    build_databases_config,
    build_django_settings_dict,
    ensure_django_configured,
    get_asgi_application,
    get_wsgi_application,
    parse_database_url,
    use_web_services,
)

# region Constants
HTTP_OK = 200
HTTP_NOT_FOUND = 404
POSTGRES_PORT = 5432
SAMPLE_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SAMPLE_W3C_TRACEPARENT = f"00-{SAMPLE_TRACE_ID}-00f067aa0ba902b7-01"
SAMPLE_HUB_SECRET = "hub-explicit-django-secret-key-0123456789abcdef"
# endregion


# region Fixtures
def _make_isolated_settings(tmp_path: Path, *, mode: str = "node") -> WorktrackSettings:
    """Construct isolated `WorktrackSettings` rooted under `tmp_path`."""
    return WorktrackSettings(
        config_file=tmp_path / "cfg" / "config.toml",
        db_path=tmp_path / "data" / "ledger.sqlite3",
        socket_path=tmp_path / "run" / "wt.sock",
        telemetry_dir=tmp_path / "telemetry",
        mode=mode,
    )


# endregion


# region Web Settings & Middleware Tests
def test_parse_database_url_supports_postgres_and_sqlite_and_rejects_invalid(
    tmp_path: Path,
) -> None:
    """Verify `parse_database_url` and `build_databases_config` handle Postgres and SQLite."""
    pg_cfg = parse_database_url(
        "postgresql://wt_user:s3cr%40t@db.internal:5432/worktrack_hub?sslmode=require"
    )
    assert pg_cfg["ENGINE"] == "django.db.backends.postgresql"
    assert pg_cfg["NAME"] == "worktrack_hub"
    assert pg_cfg["USER"] == "wt_user"
    assert pg_cfg["PASSWORD"] == "s3cr@t"
    assert pg_cfg["HOST"] == "db.internal"
    assert pg_cfg["PORT"] == str(POSTGRES_PORT)
    assert pg_cfg["OPTIONS"] == {"sslmode": "require"}

    pg_short = parse_database_url("postgres://hub/wt")
    assert pg_short["ENGINE"] == "django.db.backends.postgresql"
    assert (pg_short["HOST"], pg_short["NAME"]) == ("hub", "wt")

    sqlite_file = tmp_path / "custom.sqlite3"
    sqlite_cfg = parse_database_url(f"sqlite:///{sqlite_file.as_posix().lstrip('/')}")
    assert sqlite_cfg["ENGINE"] == "django.db.backends.sqlite3"
    assert Path(str(sqlite_cfg["NAME"])) == sqlite_file

    mem_cfg = parse_database_url("sqlite:///:memory:")
    assert mem_cfg == {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}

    for invalid_url in ("", "mysql://localhost/db", "postgresql://localhost"):
        with pytest.raises(ConfigurationError):
            parse_database_url(invalid_url)

    settings = _make_isolated_settings(tmp_path)
    default_dbs = build_databases_config(settings, environ={})
    assert default_dbs["default"] == {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(settings.db_path),
    }


def test_django_settings_node_vs_hub_secret_key_and_spectacular_oas31(tmp_path: Path) -> None:
    """Verify `node` mode mints ephemeral key while `hub` mode enforces explicit secret key."""
    node_settings = _make_isolated_settings(tmp_path, mode="node")
    node_cfg = build_django_settings_dict(environ={}, worktrack_settings=node_settings)
    assert isinstance(node_cfg["SECRET_KEY"], str) and len(node_cfg["SECRET_KEY"]) >= 32
    assert node_cfg["SPECTACULAR_SETTINGS"]["OAS_VERSION"] == "3.1.0"
    assert "127.0.0.1" in node_cfg["ALLOWED_HOSTS"]

    hub_settings = _make_isolated_settings(tmp_path, mode="hub")
    with pytest.raises(ConfigurationError):
        build_django_settings_dict(
            environ={"WORKTRACK_MODE": "hub"}, worktrack_settings=hub_settings
        )

    hub_cfg = build_django_settings_dict(
        environ={
            "WORKTRACK_MODE": "hub",
            "WORKTRACK_DJANGO_SECRET_KEY": SAMPLE_HUB_SECRET,
            "WORKTRACK_ALLOWED_HOSTS": "hub.example.org,api.example.org",
            "DATABASE_URL": "postgresql://wt:pw@pg.internal:5432/wt_hub",
        },
        worktrack_settings=hub_settings,
    )
    assert hub_cfg["SECRET_KEY"] == SAMPLE_HUB_SECRET
    assert hub_cfg["ALLOWED_HOSTS"] == ["hub.example.org", "api.example.org"]
    assert hub_cfg["DATABASES"]["default"]["ENGINE"] == "django.db.backends.postgresql"


def test_ensure_django_configured_idempotent_and_wsgi_asgi_entrypoints() -> None:
    """Verify `ensure_django_configured()` is idempotent and exposes WSGI/ASGI callables."""
    ensure_django_configured()
    ensure_django_configured()
    assert callable(get_wsgi_application())
    assert callable(get_asgi_application())


def test_healthz_metrics_and_trace_middleware_bounded_route_labels(tmp_path: Path) -> None:
    """Verify `/healthz`, `/metrics`, `X-Fritz-Trace-Id`, JSONL events, and bounded route labels."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    container = WebServiceContainer.from_settings(_make_isolated_settings(tmp_path))
    missing_uuid = "550e8400-e29b-41d4-a716-446655440000"

    with use_web_services(container):
        client = APIClient()
        health = client.get("/healthz", HTTP_TRACEPARENT=SAMPLE_W3C_TRACEPARENT)
        assert health.status_code == HTTP_OK and health["X-Fritz-Trace-Id"] == SAMPLE_TRACE_ID
        assert health.json() == {"status": "ok", "mode": "node", "namespace": "worktrack"}

        miss = client.get(
            f"/api/v1/activities/{missing_uuid}/", HTTP_X_FRITZ_TRACE_ID=SAMPLE_TRACE_ID
        )
        assert miss.status_code == HTTP_NOT_FOUND and miss["X-Fritz-Trace-Id"] == SAMPLE_TRACE_ID
        assert client.get(f"/nonexistent/{missing_uuid}/path").status_code == HTTP_NOT_FOUND

        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == HTTP_OK
        exposition = metrics_resp.content.decode("utf-8")

    for expected in (
        'worktrack_http_requests_total{method="GET",route="healthz",status_class="2xx"} 1.0',
        (
            'worktrack_http_requests_total{method="GET",'
            'route="api_v1_activities",status_class="4xx"} 1.0'
        ),
        'worktrack_http_requests_total{method="GET",route="unmatched",status_class="4xx"} 1.0',
        'worktrack_http_request_duration_seconds_count{method="GET",route="healthz"} 1.0',
    ):
        assert expected in exposition
    assert missing_uuid not in exposition

    events = [
        json.loads(line)
        for line in container.event_logger.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(ev["trace_id"] == SAMPLE_TRACE_ID for ev in events)


def test_lazy_web_imports_and_fresh_django_setup_in_subprocess(tmp_path: Path) -> None:
    """Verify `worktrack.web.database` does not trigger `django.setup()` and setup is safe."""
    base_env = {
        **os.environ,
        "WORKTRACK_DB_PATH": str(tmp_path / "data" / "ledger.sqlite3"),
        "WORKTRACK_TELEMETRY_DIR": str(tmp_path / "telemetry"),
    }
    base_env.pop("WORKTRACK_DJANGO_SECRET_KEY", None)
    base_env.pop("WORKTRACK_DJANGO_SECRET_KEY_URI", None)

    hub_no_secret = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from django.apps import apps; "
                "from worktrack.web.database import parse_database_url; "
                "assert not apps.ready; "
                "assert parse_database_url('sqlite:///:memory:')['NAME'] == ':memory:'"
            ),
        ],
        env={**base_env, "WORKTRACK_MODE": "hub"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert hub_no_secret.returncode == 0, hub_no_secret.stderr

    fresh_setup = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, django; "
                "os.environ['DJANGO_SETTINGS_MODULE'] = 'worktrack.web.settings'; "
                "django.setup()"
            ),
        ],
        env={**base_env, "WORKTRACK_MODE": "node"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert fresh_setup.returncode == 0, fresh_setup.stderr


def test_hub_mode_postgres_url_startup_healthz_metrics_and_oauth_subprocess(
    tmp_path: Path,
) -> None:
    """Verify Hub mode with `DATABASE_URL=postgresql://...` serves `--check`, healthz, and OAuth."""
    hub_env = {
        **os.environ,
        "WORKTRACK_MODE": "hub",
        "DATABASE_URL": "postgresql://user:pass@db.example.internal:5432/worktrack",
        "WORKTRACK_DJANGO_SECRET_KEY": SAMPLE_HUB_SECRET,
        "WORKTRACK_DB_PATH": str(tmp_path / "data" / "ledger.sqlite3"),
        "WORKTRACK_TELEMETRY_DIR": str(tmp_path / "telemetry"),
        "WORKTRACK_OAUTH_JIRA_CLIENT_ID": "hub-jira-client",
        "WORKTRACK_OAUTH_JIRA_CLIENT_SECRET_URI": "env://HUB_JIRA_SECRET",
        "WORKTRACK_OAUTH_JIRA_TOKEN_ENDPOINT": "https://auth.atlassian.com/oauth/token",
        "HUB_JIRA_SECRET": "hub-secret-val",
    }
    script = (
        "import io, json; "
        "from worktrack.cli import main; "
        "from worktrack.secrets import "
        "OAuthHttpResponse, UrllibOAuthHttpTransport, generate_pkce_pair; "
        "from worktrack.web import ensure_django_configured; "
        "out = io.StringIO(); "
        "assert main(['serve', '--check', '--json'], stdout=out) == 0; "
        "assert json.loads(out.getvalue())['mode'] == 'hub'; "
        "ensure_django_configured(); "
        "from rest_framework.test import APIClient; "
        "UrllibOAuthHttpTransport.post_form = lambda self, url, params, *, timeout_seconds: "
        "OAuthHttpResponse(status_code=200, "
        "payload={'access_token': 'tok-1', 'token_type': 'Bearer'}); "
        "c = APIClient(HTTP_HOST='127.0.0.1'); "
        "assert c.get('/healthz').json() == "
        "{'status': 'ok', 'mode': 'hub', 'namespace': 'worktrack'}; "
        "pkce = generate_pkce_pair(); "
        "r = c.post('/api/v1/oauth/jira/exchange/', "
        "{'code': 'c1', 'code_verifier': pkce.code_verifier, "
        "'redirect_uri': 'http://127.0.0.1:5173/cb'}, format='json'); "
        "assert r.status_code == 200 and r['Cache-Control'] == 'no-store'; "
        "assert c.get('/api/v1/schema/').status_code == 200; "
        "assert c.get('/metrics').status_code == 200"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], env=hub_env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


# endregion
