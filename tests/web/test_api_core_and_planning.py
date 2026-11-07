"""Behavioural tests for `/api/v1/` Core Ledger and Horizon Planning DRF endpoints."""

from __future__ import annotations

from pathlib import Path

from worktrack.config import WorktrackSettings
from worktrack.web import WebServiceContainer, ensure_django_configured, use_web_services

# region Constants
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_NO_CONTENT = 204
HTTP_CONFLICT = 409
EXPECTED_SPLIT_COUNT = 2
HALF_HOUR_SECONDS = 1_800
ONE_HOUR_SECONDS = 3_600
FULL_WEIGHT = 1.0
CUSTOM_FOCUS_RATING = 4
BUDGET_MINUTES = 90
PRIORITY_HIGH = 2
# endregion


# region Fixtures
def _build_container(tmp_path: Path) -> WebServiceContainer:
    """Create an isolated `WebServiceContainer` backed by `tmp_path`."""
    settings = WorktrackSettings(
        config_file=tmp_path / "cfg" / "config.toml",
        db_path=tmp_path / "data" / "ledger.sqlite3",
        socket_path=tmp_path / "run" / "wt.sock",
        telemetry_dir=tmp_path / "telemetry",
    )
    return WebServiceContainer.from_settings(settings)


# endregion


# region Core & Planning API Tests
def test_activities_and_clock_lifecycle_via_drf_api(tmp_path: Path) -> None:
    """Verify `/api/v1/activities/` CRUD and `/api/v1/intervals/clock-{in,out}/` lifecycle."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    with use_web_services(_build_container(tmp_path)):
        client = APIClient()
        act_resp = client.post(
            "/api/v1/activities/",
            {"name": "Build OAuth Broker", "kind": "targeted", "external_ref": "PROJ-701"},
            format="json",
        )
        assert act_resp.status_code == HTTP_CREATED
        act_id = act_resp.json()["id"]

        patch_resp = client.patch(
            f"/api/v1/activities/{act_id}/",
            {"rollup_target_ref": "PROJ-701"},
            format="json",
        )
        assert patch_resp.status_code == HTTP_OK
        assert patch_resp.json()["rollup_target_ref"] == "PROJ-701"
        assert len(client.get("/api/v1/activities/").json()) == 1

        in_resp = client.post(
            "/api/v1/intervals/clock-in/",
            {"activity_id": act_id, "started_at": "2026-11-07T08:00:00Z"},
            format="json",
        )
        assert in_resp.status_code == HTTP_CREATED and in_resp.json()["is_open"] is True

        conflict_resp = client.post(
            "/api/v1/intervals/clock-in/",
            {"activity_id": act_id, "started_at": "2026-11-07T08:15:00Z"},
            format="json",
        )
        assert conflict_resp.status_code == HTTP_CONFLICT

        out_resp = client.post(
            "/api/v1/intervals/clock-out/",
            {"ended_at": "2026-11-07T09:00:00Z"},
            format="json",
        )
        assert out_resp.status_code == HTTP_OK
        assert out_resp.json()["duration_seconds"] == float(ONE_HOUR_SECONDS)


def test_chaotic_intervals_split_and_bind_via_drf_api(tmp_path: Path) -> None:
    """Verify chaotic `/api/v1/intervals/` capture, `split`, and `bind` endpoints."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    with use_web_services(_build_container(tmp_path)):
        client = APIClient()
        act_id = client.post(
            "/api/v1/activities/",
            {"name": "RetroSplit Target", "kind": "targeted"},
            format="json",
        ).json()["id"]
        chaotic_resp = client.post(
            "/api/v1/intervals/",
            {
                "capture_mode": "chaotic",
                "started_at": "2026-11-07T10:00:00Z",
                "ended_at": "2026-11-07T11:00:00Z",
                "notes": "Deep work block",
            },
            format="json",
        )
        assert chaotic_resp.status_code == HTTP_CREATED
        chaotic_id = chaotic_resp.json()["id"]

        split_resp = client.post(
            f"/api/v1/intervals/{chaotic_id}/split/",
            {
                "slices": [
                    {"activity_id": act_id, "duration_seconds": HALF_HOUR_SECONDS},
                    {"activity_id": None, "duration_seconds": HALF_HOUR_SECONDS},
                ]
            },
            format="json",
        )
        assert split_resp.status_code == HTTP_OK
        slices = split_resp.json()
        assert len(slices) == EXPECTED_SPLIT_COUNT

        bind_resp = client.post(
            f"/api/v1/intervals/{slices[1]['id']}/bind/",
            {"activity_id": act_id},
            format="json",
        )
        assert bind_resp.status_code == HTTP_OK and bind_resp.json()["activity_id"] == act_id


def test_entities_and_attribution_shares_endpoints(tmp_path: Path) -> None:
    """Verify `/api/v1/entities/` and `/api/v1/attributions/{,shares/}` endpoints."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    with use_web_services(_build_container(tmp_path)):
        client = APIClient()
        act_id = client.post(
            "/api/v1/activities/",
            {"name": "API Schema Export", "kind": "targeted", "external_ref": "PROJ-702"},
            format="json",
        ).json()["id"]
        client.post(
            "/api/v1/intervals/",
            {
                "capture_mode": "structured",
                "activity_id": act_id,
                "started_at": "2026-11-07T13:00:00Z",
                "ended_at": "2026-11-07T14:00:00Z",
            },
            format="json",
        )
        ent_resp = client.post(
            "/api/v1/entities/",
            {"kind": "project", "key": "PROJ", "name": "Worktrack Platform"},
            format="json",
        )
        assert ent_resp.status_code == HTTP_CREATED
        ent_id = ent_resp.json()["id"]
        assert client.get(f"/api/v1/entities/{ent_id}/").status_code == HTTP_OK

        attr_resp = client.post(
            "/api/v1/attributions/",
            {"target_entity_id": ent_id, "source_activity_id": act_id, "weight": FULL_WEIGHT},
            format="json",
        )
        assert attr_resp.status_code == HTTP_CREATED
        shares = client.get("/api/v1/attributions/shares/?target_kind=project").json()
        assert len(shares) == 1 and shares[0]["attributed_seconds"] == float(ONE_HOUR_SECONDS)
        assert client.delete(f"/api/v1/attributions/{attr_resp.json()['id']}/").status_code == (
            HTTP_NO_CONTENT
        )


def test_facets_and_context_facts_endpoints(tmp_path: Path) -> None:
    """Verify `/api/v1/facets/` registration and `/api/v1/context-facts/` assertion."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    with use_web_services(_build_container(tmp_path)):
        client = APIClient()
        facet_resp = client.post(
            "/api/v1/facets/",
            {"key": "focus_rating", "value_type": "int", "description": "1-5 rating"},
            format="json",
        )
        assert facet_resp.status_code == HTTP_CREATED
        assert client.get("/api/v1/facets/workplace/").status_code == HTTP_OK

        fact_resp = client.post(
            "/api/v1/context-facts/",
            {
                "facet_key": "focus_rating",
                "value": CUSTOM_FOCUS_RATING,
                "scope_date": "2026-11-07",
            },
            format="json",
        )
        assert fact_resp.status_code == HTTP_CREATED
        facts = client.get("/api/v1/context-facts/?facet_key=focus_rating").json()
        assert len(facts) == 1 and facts[0]["value"] == CUSTOM_FOCUS_RATING


def test_agenda_items_focus_blocks_and_horizon_insights_endpoints(tmp_path: Path) -> None:
    """Verify `/api/v1/agenda/*` and `/api/v1/insights/{now,today,week}/` endpoints."""
    ensure_django_configured()
    from rest_framework.test import APIClient

    with use_web_services(_build_container(tmp_path)):
        client = APIClient()
        act_id = client.post(
            "/api/v1/activities/",
            {"name": "Ship WT-WP-07", "kind": "targeted", "external_ref": "PROJ-707"},
            format="json",
        ).json()["id"]
        item_id = client.post(
            "/api/v1/agenda/items/",
            {
                "activity_id": act_id,
                "horizon": "today",
                "priority": PRIORITY_HIGH,
                "daily_budget_minutes": BUDGET_MINUTES,
            },
            format="json",
        ).json()["id"]
        block_id = client.post(
            "/api/v1/agenda/focus-blocks/",
            {
                "started_at": "2026-11-07T10:00:00Z",
                "ended_at": "2026-11-07T11:30:00Z",
                "activity_id": act_id,
                "label": "API Focus",
                "expose_busy": True,
            },
            format="json",
        ).json()["id"]

        now_resp = client.get("/api/v1/insights/now/?now=2026-11-07T10:15:00Z")
        today_resp = client.get("/api/v1/insights/today/?now=2026-11-07T10:15:00Z")
        week_resp = client.get("/api/v1/insights/week/?now=2026-11-07T10:15:00Z")
        assert now_resp.json()["active_focus_block"]["id"] == block_id
        assert today_resp.json()["horizon"] == "today"
        assert week_resp.json()["horizon"] == "this_week"

        done_resp = client.patch(f"/api/v1/agenda/items/{item_id}/", {"status": "done"}, "json")
        assert done_resp.status_code == HTTP_OK and done_resp.json()["status"] == "done"
        assert (
            client.delete(f"/api/v1/agenda/focus-blocks/{block_id}/").status_code == HTTP_NO_CONTENT
        )


# endregion
