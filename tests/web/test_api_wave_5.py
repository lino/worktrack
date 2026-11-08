"""Behavioural tests for Wave 5 DRF reporting and absence stack endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from worktrack.config import WorktrackSettings
from worktrack.web import (
    WebServiceContainer,
    ensure_django_configured,
    reset_default_web_services,
    use_web_services,
)
from worktrack.web.schema_export import validate_openapi_schema_zero_warnings

# region Constants
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404

DATE_VACATION_START = "2026-08-03"
DATE_VACATION_END = "2026-08-07"
SUBDIVISION_BAVARIA = "DE-BY"
PACK_GERMANY = "de"
PACK_GENERIC = "generic"
YEAR_2026 = 2026
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
    ensure_django_configured()
    from rest_framework.test import APIClient

    container = WebServiceContainer.from_settings(settings, environ=env)
    with use_web_services(container):
        yield APIClient(), container
    reset_default_web_services()


# endregion


# region Web Tests
def test_reporting_packs_list(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify GET /api/v1/reporting/packs/ lists available reporting packs."""
    client, _ = web_client_and_services
    resp = client.get("/api/v1/reporting/packs/")
    assert resp.status_code == HTTP_OK
    data = resp.json()
    assert "packs" in data
    slugs = [p["slug"] for p in data["packs"]]
    assert PACK_GERMANY in slugs
    assert PACK_GENERIC in slugs


def test_reporting_holidays_list(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify GET /api/v1/reporting/holidays/ returns statutory holidays."""
    client, _ = web_client_and_services
    resp = client.get(
        f"/api/v1/reporting/holidays/?year={YEAR_2026}&subdivision={SUBDIVISION_BAVARIA}"
    )
    assert resp.status_code == HTTP_OK
    data = resp.json()
    assert "holidays" in data
    assert any(h["name"] == "Neujahr" for h in data["holidays"])
    assert any(h["name"] == "Heilige Drei Könige" for h in data["holidays"])


def test_reporting_quota_lifecycle(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify vacation quota updates and retrieval via DRF endpoints."""
    client, _ = web_client_and_services

    # 1. Update quota
    put_resp = client.put(
        f"/api/v1/reporting/absences/quota/?year={YEAR_2026}",
        {"entitlement_days": 30.0, "carryover_days": 2.0},
        format="json",
    )
    assert put_resp.status_code == HTTP_OK
    quota_data = put_resp.json()
    assert quota_data["total_available_days"] == 32.0

    # 2. Retrieve quota
    get_quota = client.get(f"/api/v1/reporting/absences/quota/?year={YEAR_2026}")
    assert get_quota.status_code == HTTP_OK
    assert get_quota.json()["entitlement_days"] == 30.0


def test_reporting_absences_and_summary_lifecycle(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify absence creation, listing, and annual summary via DRF endpoints."""
    client, _ = web_client_and_services

    # Pre-set quota so summary has total quota
    client.put(
        f"/api/v1/reporting/absences/quota/?year={YEAR_2026}",
        {"entitlement_days": 30.0, "carryover_days": 2.0},
        format="json",
    )

    # 1. Create absence
    post_abs = client.post(
        "/api/v1/reporting/absences/",
        {
            "start_date": DATE_VACATION_START,
            "end_date": DATE_VACATION_END,
            "kind": "vacation",
            "day_fraction": 1.0,
            "notes": "Summer holiday",
            "subdivision": SUBDIVISION_BAVARIA,
        },
        format="json",
    )
    assert post_abs.status_code == HTTP_CREATED
    abs_data = post_abs.json()
    assert abs_data["effective_days"] == 5.0

    # 2. List absences
    get_abs = client.get(
        f"/api/v1/reporting/absences/?start_date=2026-08-01&end_date=2026-08-31&subdivision={SUBDIVISION_BAVARIA}"
    )
    assert get_abs.status_code == HTTP_OK
    assert get_abs.json()["count"] == 1

    # 3. Summary
    get_sum = client.get(
        f"/api/v1/reporting/absences/summary/?year={YEAR_2026}&subdivision={SUBDIVISION_BAVARIA}&reference_date=2026-08-05"
    )
    assert get_sum.status_code == HTTP_OK
    sum_data = get_sum.json()
    assert sum_data["taken_vacation_days"] == 3.0
    assert sum_data["planned_vacation_days"] == 2.0
    assert sum_data["remaining_vacation_days"] == 27.0


def test_reporting_travel_trips_lifecycle(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify travel trip creation, allowances, and listing via DRF endpoints."""
    client, _ = web_client_and_services

    post_resp = client.post(
        "/api/v1/reporting/travel-trips/",
        {
            "title": "Strategy Onsite",
            "destination": "Berlin",
            "started_at": "2026-10-12T08:00:00Z",
            "ended_at": "2026-10-14T18:00:00Z",
            "country_code": "DE",
            "transport_mode": "car",
            "distance_km": 300.0,
            "breakfast_dates": ["2026-10-13", "2026-10-14"],
            "lunch_dates": ["2026-10-13"],
            "pack": PACK_GERMANY,
        },
        format="json",
    )
    assert post_resp.status_code == HTTP_CREATED
    calc = post_resp.json()
    assert calc["trip"]["destination"] == "Berlin"
    assert calc["mileage_allowance"] == 90.0  # 300 * 0.30
    assert calc["gross_per_diem_total"] == 56.0
    assert calc["net_per_diem_total"] == 33.6

    get_resp = client.get(
        f"/api/v1/reporting/travel-trips/?start_date=2026-10-01&end_date=2026-10-31&pack={PACK_GERMANY}"
    )
    assert get_resp.status_code == HTTP_OK
    assert get_resp.json()["count"] == 1


def test_reporting_summary_tax_exclusions(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify statutory summary excludes vacations and public holidays from office/home tax days."""
    client, services = web_client_and_services

    # Set up facts in ledger:
    # 2026-01-01 is Neujahr (Public holiday in Bavaria)
    # 2026-08-03 is Vacation
    # 2026-09-01 is regular home_office day
    # 2026-09-02 is regular office day
    with services.open_ledger() as ledger:
        ledger.create_activity("Project Work")
        ledger.assert_context_fact(
            "workplace", "home_office", scope_date="2026-01-01"
        )  # Holiday! Excluded
        ledger.assert_context_fact(
            "workplace", "home_office", scope_date="2026-08-03"
        )  # Vacation! Excluded
        ledger.assert_context_fact(
            "workplace", "home_office", scope_date="2026-09-01"
        )  # Eligible home_office
        ledger.assert_context_fact(
            "workplace", "office", scope_date="2026-09-02"
        )  # Eligible office

    # Record vacation for 2026-08-03..2026-08-07
    client.post(
        "/api/v1/reporting/absences/",
        {
            "start_date": "2026-08-03",
            "end_date": "2026-08-07",
            "kind": "vacation",
            "day_fraction": 1.0,
            "subdivision": SUBDIVISION_BAVARIA,
        },
        format="json",
    )

    # Query summary
    resp = client.get(
        f"/api/v1/reporting/summary/?start_date=2026-01-01&end_date=2026-12-31"
        f"&pack={PACK_GERMANY}&subdivision={SUBDIVISION_BAVARIA}&office_one_way_km=20&report_format=json"
    )
    assert resp.status_code == HTTP_OK
    data = resp.json()
    assert data["home_office_days_raw"] == 3
    # 2026-01-01 (holiday) and 2026-08-03 (vacation) must be excluded from eligible!
    assert data["home_office_days_eligible"] == 1
    assert data["home_office_allowance"] == 6.00
    assert data["office_days_eligible"] == 1
    assert data["commute_allowance_total"] == 6.00  # 20 * 0.30
    assert "rendered" in data
    assert data["report_format"] == "json"


def test_reporting_summary_rendered_formats(
    web_client_and_services: tuple[Any, WebServiceContainer],
) -> None:
    """Verify statutory summary renders CSV and markdown formats via DRF view."""
    client, services = web_client_and_services
    with services.open_ledger() as ledger:
        ledger.create_activity("Project Work")
        ledger.assert_context_fact("workplace", "home_office", scope_date="2026-09-01")

    # Query CSV rendered format
    resp_csv = client.get(
        f"/api/v1/reporting/summary/?start_date=2026-09-01&end_date=2026-09-30"
        f"&pack={PACK_GERMANY}&subdivision={SUBDIVISION_BAVARIA}&office_one_way_km=20&report_format=csv"
    )
    assert resp_csv.status_code == HTTP_OK
    csv_data = resp_csv.json()
    assert "section,item,metric,value,unit" in csv_data["rendered"]

    # Query Markdown rendered format
    resp_md = client.get(
        f"/api/v1/reporting/summary/?start_date=2026-09-01&end_date=2026-09-30"
        f"&pack={PACK_GERMANY}&subdivision={SUBDIVISION_BAVARIA}&office_one_way_km=20&report_format=markdown"
    )
    assert resp_md.status_code == HTTP_OK
    md_data = resp_md.json()
    assert "# Jurisdiction Report:" in md_data["rendered"]


def test_reporting_openapi_schema_zero_warnings() -> None:
    """Verify that OpenAPI 3.1 schema generates with zero warnings or errors."""
    diagnostics = validate_openapi_schema_zero_warnings()
    assert diagnostics == []


# endregion
