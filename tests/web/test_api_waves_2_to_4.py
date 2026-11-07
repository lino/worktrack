"""Behavioural DRF endpoint and error-mapping tests for Wave 2-4 OpenAPI routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from worktrack.collectors import ClueNotFoundError, CollectorError, ForgeAuthenticationError
from worktrack.config import WorktrackSettings
from worktrack.connectors.calendar import CalendarAuthenticationError, CalendarConnectorError
from worktrack.exporters import ExporterError
from worktrack.hub_sync import HubAuthenticationError, HubEgressBypassError, HubSyncError
from worktrack.mcp import McpEgressBypassError
from worktrack.reconciler import ReconcilerError, RetroSplitConflictError
from worktrack.sensors import SensorError
from worktrack.web import (
    ROUTE_LABEL_BY_URL_NAME,
    WebServiceContainer,
    ensure_django_configured,
    reset_default_web_services,
    use_web_services,
    worktrack_exception_handler,
)

# region Constants
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409

SAMPLE_ICS_TEXT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Worktrack//WebTest//EN
BEGIN:VEVENT
UID:evt-web-801@example.test
SUMMARY:PROJ-801 Sprint Review
DTSTART:20261107T140000Z
DTEND:20261107T150000Z
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""
# endregion


# region Fixtures
@pytest.fixture
def web_client_and_services(tmp_path: Path) -> Iterator[tuple[Any, WebServiceContainer]]:
    """Yield `(APIClient, WebServiceContainer)` backed by an isolated SQLite database."""
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "WORKTRACK_DB_PATH": str(tmp_path / "data" / "ledger.sqlite3"),
        "WORKTRACK_SOCKET_PATH": str(tmp_path / "run" / "wt.sock"),
        "WORKTRACK_TELEMETRY_DIR": str(tmp_path / "telemetry"),
        "WORKTRACK_TOKEN_STORE_PATH": str(tmp_path / "cfg" / "oauth_tokens.json"),
    }
    settings = WorktrackSettings.from_env(environ=env)
    ensure_django_configured(environ=env, worktrack_settings=settings)
    from rest_framework.test import APIClient

    container = WebServiceContainer.from_settings(settings, environ=env)
    client: Any = APIClient()
    with use_web_services(container):
        yield client, container
    reset_default_web_services()


# endregion


# region Wave 2-4 API Tests
def test_clues_and_sensors_api_endpoints(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify `/api/v1/clues/`, `/api/v1/clues/git-sync/`, and `/api/v1/sensors/evaluate/`."""
    client, _ = web_client_and_services
    act_resp = client.post(
        "/api/v1/activities/",
        {"name": "Ship Wave 2 API", "kind": "targeted", "external_ref": "PROJ-801"},
        format="json",
    )
    assert act_resp.status_code == HTTP_CREATED
    act_id = act_resp.json()["id"]

    clue_payload = {
        "source": "git",
        "external_id": "commit-801",
        "occurred_at": "2026-11-07T09:15:00Z",
        "summary": "PROJ-801 Add clues endpoint",
        "inferred_activity_key": "PROJ-801",
        "activity_id": act_id,
        "confidence": 0.9,
    }
    sync_resp = client.post("/api/v1/clues/git-sync/", {"clues": [clue_payload]}, format="json")
    assert sync_resp.status_code == HTTP_OK and sync_resp.json()["clues_upserted"] == 1

    list_resp = client.get(
        "/api/v1/clues/?source=git&start=2026-11-07T00:00:00Z&end=2026-11-07T23:59:59Z"
    )
    assert list_resp.status_code == HTTP_OK
    clues = list_resp.json()["clues"]
    assert len(clues) == 1 and clues[0]["external_id"] == "commit-801"

    rule = {
        "rule_id": "office-cidr",
        "workplace": "office",
        "ipv4_cidrs": ["10.20.0.0/16"],
        "confidence": 0.95,
    }
    snap = {
        "captured_at": "2026-11-07T09:00:00Z",
        "ipv4_addresses": ["10.20.4.15"],
        "wifi_ssid": "CorpNet",
        "displays": ["Dell U2723QE"],
    }
    sensor_resp = client.post(
        "/api/v1/sensors/evaluate/",
        {"scope_date": "2026-11-07", "apply": True, "rules": [rule], "snapshot": snap},
        format="json",
    )
    assert sensor_resp.status_code == HTTP_OK
    sensor_body = sensor_resp.json()
    assert sensor_body["matched"] is True and sensor_body["suggestion"]["workplace"] == "office"
    assert sensor_body["suggestion"]["context_fact_id"] is not None


def test_reconciler_inspect_propose_and_apply_api_endpoints(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify `/api/v1/reconciler/{inspect,propose,apply}/` endpoints."""
    client, services = web_client_and_services
    with services.open_ledger() as ledger, services.open_clue_store() as clue_store:
        act_1 = ledger.create_activity("Task 1", external_ref="PROJ-811")
        act_2 = ledger.create_activity("Task 2", external_ref="PROJ-812")
        chaotic = ledger.record_chaotic_interval(
            started_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
            ended_at=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
            notes="Morning unallocated work",
        )
        clue_store.upsert_clue(
            source="git",
            external_key="sha-811",
            occurred_at=datetime(2026, 11, 7, 10, 20, tzinfo=UTC),
            summary="PROJ-811 commit",
            inferred_activity_key="PROJ-811",
            activity_id=act_1.id,
            confidence=0.9,
        )

    inspect_resp = client.get(
        "/api/v1/reconciler/inspect/?start=2026-11-07T00:00:00Z&end=2026-11-07T23:59:59Z"
    )
    assert inspect_resp.status_code == HTTP_OK
    insp = inspect_resp.json()
    assert insp["total_unallocated_seconds"] == 3600 and len(insp["candidate_clues"]) == 1

    prop_resp = client.post(
        "/api/v1/reconciler/propose/",
        {
            "interval_id": chaotic.id,
            "activity_ids": [act_1.id, act_2.id],
            "strategy": "weights",
            "weights": {act_1.id: 1.0, act_2.id: 3.0},
        },
        format="json",
    )
    assert prop_resp.status_code == HTTP_OK
    assert [s["duration_seconds"] for s in prop_resp.json()["proposal"]["slices"]] == [900, 2700]

    apply_resp = client.post(
        "/api/v1/reconciler/apply/",
        {"interval_id": chaotic.id, "activity_ids": [act_1.id, act_2.id], "strategy": "equal"},
        format="json",
    )
    assert apply_resp.status_code == HTTP_OK
    created = apply_resp.json()["created_intervals"]
    assert len(created) == 2 and [c["duration_seconds"] for c in created] == [1800, 1800]


def test_calendar_sync_and_stage_focus_api_endpoints(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify `/api/v1/calendar/sync/` and `/api/v1/calendar/stage-focus/` endpoints."""
    client, _ = web_client_and_services
    sync_resp = client.post(
        "/api/v1/calendar/sync/",
        {"ics_text": SAMPLE_ICS_TEXT, "standing_activity_name": "Meetings & Alignment"},
        format="json",
    )
    assert sync_resp.status_code == HTTP_OK
    assert sync_resp.json()["events_synced"] == 1 and sync_resp.json()["intervals_created"] == 1

    fb_resp = client.post(
        "/api/v1/agenda/focus-blocks/",
        {
            "started_at": "2026-11-07T15:00:00Z",
            "ended_at": "2026-11-07T16:00:00Z",
            "label": "Confidential Architecture Review",
            "expose_busy": True,
        },
        format="json",
    )
    assert fb_resp.status_code == HTTP_CREATED

    stage_resp = client.post(
        "/api/v1/calendar/stage-focus/",
        {"start": "2026-11-07T00:00:00Z", "end": "2026-11-07T23:59:59Z", "target_ref": "primary"},
        format="json",
    )
    assert stage_resp.status_code == HTTP_CREATED
    staged_items = stage_resp.json()["staged_items"]
    assert len(staged_items) == 1 and staged_items[0]["sink"] == "calendar"
    assert staged_items[0]["payload"]["busy_summary"] == "Focus Time"


def test_hub_rollups_and_catalog_api_endpoints(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify `/api/v1/hub/rollups/` and `/api/v1/hub/catalog/` endpoints."""
    client, _ = web_client_and_services
    tmpl = {
        "target_kind": "client",
        "target_key": "acme",
        "weight": 1.0,
        "source_activity_ref": "PROJ-901",
    }
    cat_post = client.post(
        "/api/v1/hub/catalog/",
        {
            "entities": [{"kind": "client", "key": "acme", "name": "Acme Corp", "metadata": {}}],
            "attribution_templates": [tmpl],
        },
        format="json",
    )
    assert cat_post.status_code == HTTP_OK and len(cat_post.json()["entities"]) == 1

    cat_get = client.get("/api/v1/hub/catalog/")
    assert cat_get.status_code == HTTP_OK and cat_get.json()["entities"][0]["key"] == "acme"

    rollup_body = {
        "schema": "worktrack.hub.rollup.v1",
        "node_id": "node-1",
        "idempotency_key": "idem-901",
        "target_ref": "hub:eng",
        "grouping_window": "day",
        "window_start": "2026-11-07T00:00:00Z",
        "window_end": "2026-11-08T00:00:00Z",
        "total_rounded_seconds": 7200,
        "buckets": [],
        "entity_shares_seconds": {"client:acme": 7200},
    }
    rollup_post = client.post("/api/v1/hub/rollups/", rollup_body, format="json")
    assert rollup_post.status_code == HTTP_CREATED and rollup_post.json()["node_id"] == "node-1"

    rollup_list = client.get("/api/v1/hub/rollups/?node_id=node-1")
    assert rollup_list.status_code == HTTP_OK and len(rollup_list.json()["rollups"]) == 1


def test_exports_workplace_tax_and_attribution_api_endpoints(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify `/api/v1/exports/workplace-tax/` and `/api/v1/exports/entity-attribution/`."""
    client, services = web_client_and_services
    with services.open_ledger() as ledger:
        act = ledger.create_activity("Hub Export Work", external_ref="PROJ-901")
        ent = ledger.create_entity("client", "acme", "Acme Corp")
        ledger.set_attribution(target_entity_id=ent.id, weight=1.0, source_activity_id=act.id)
        ledger.record_structured_interval(
            act.id,
            started_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
            ended_at=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
        )
        ledger.assert_context_fact("workplace", "home_office", scope_date="2026-11-07")

    tax_resp = client.get("/api/v1/exports/workplace-tax/?year=2026&format=markdown")
    assert tax_resp.status_code == HTTP_OK
    assert tax_resp.json()["report"]["workplace_days"]["home_office"] == 1
    assert "home_office" in tax_resp.json()["rendered"]

    attr_resp = client.get(
        "/api/v1/exports/entity-attribution/"
        "?start=2026-11-07T00:00:00Z&end=2026-11-07T23:59:59Z&entity_kind=client&format=csv"
    )
    assert attr_resp.status_code == HTTP_OK
    assert attr_resp.json()["report"]["rows"][0]["entity_key"] == "acme"


def test_wave_2_to_4_exception_classification_and_route_labels() -> None:
    """Verify domain exception status mappings and bounded route labels for Wave 2-4."""
    expected_routes = {
        "api_v1_clues_list",
        "api_v1_clues_git_sync",
        "api_v1_sensors_evaluate",
        "api_v1_reconciler_inspect",
        "api_v1_reconciler_propose",
        "api_v1_reconciler_apply",
        "api_v1_calendar_sync",
        "api_v1_calendar_stage_focus",
        "api_v1_hub_rollups",
        "api_v1_hub_catalog",
        "api_v1_exports_workplace_tax",
        "api_v1_exports_entity_attribution",
    }
    assert expected_routes <= set(ROUTE_LABEL_BY_URL_NAME.keys())

    cases: list[tuple[Exception, int, str]] = [
        (ClueNotFoundError("clue missing"), HTTP_NOT_FOUND, "not_found"),
        (HubEgressBypassError("raw notes forbidden"), HTTP_FORBIDDEN, "egress_bypass_blocked"),
        (
            McpEgressBypassError("direct sink blocked", sink="jira_cloud"),
            HTTP_FORBIDDEN,
            "egress_bypass_blocked",
        ),
        (CalendarAuthenticationError("missing token"), HTTP_UNAUTHORIZED, "authentication_error"),
        (HubAuthenticationError("invalid hub token"), HTTP_UNAUTHORIZED, "authentication_error"),
        (ForgeAuthenticationError("bad github pat"), HTTP_UNAUTHORIZED, "authentication_error"),
        (RetroSplitConflictError("interval open"), HTTP_CONFLICT, "conflict"),
        (CollectorError("bad collector input"), HTTP_BAD_REQUEST, "validation_error"),
        (SensorError("bad sensor rule"), HTTP_BAD_REQUEST, "validation_error"),
        (ReconcilerError("bad reconciler window"), HTTP_BAD_REQUEST, "validation_error"),
        (CalendarConnectorError("malformed ics"), HTTP_BAD_REQUEST, "validation_error"),
        (ExporterError("invalid year"), HTTP_BAD_REQUEST, "validation_error"),
        (HubSyncError("invalid rollup"), HTTP_BAD_REQUEST, "validation_error"),
    ]
    for exc, expected_status, expected_code in cases:
        resp = worktrack_exception_handler(exc, {})
        assert resp is not None
        assert resp.status_code == expected_status
        assert resp.data["code"] == expected_code


# endregion
