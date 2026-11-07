"""Behavioural tests for `TaxAndAttributionExporter` across CSV, JSON, and Markdown."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from worktrack.core import (
    ENTITY_KIND_COMPONENT,
    ENTITY_KIND_COST_CENTER,
    ENTITY_KIND_CUSTOMER,
    ENTITY_KIND_EPIC,
    ENTITY_KIND_PROJECT,
    Ledger,
)
from worktrack.exporters import (
    WORKPLACE_UNCLASSIFIED,
    EntityAttributionReport,
    EntityStatementRow,
    ExporterValidationError,
    ExportFormat,
    TaxAndAttributionExporter,
    WorkplaceDayEntry,
    WorkplaceSummaryRow,
    WorkplaceTaxReport,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
TAX_YEAR = 2026
DAY_ONE = date(2026, 11, 2)
DAY_TWO = date(2026, 11, 3)
DAY_THREE = date(2026, 11, 4)
DAY_FOUR = date(2026, 11, 5)
WINDOW_START = datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 11, 6, 0, 0, 0, tzinfo=UTC)

EIGHT_HOURS = 8.0
SIX_HOURS = 6.0
FOUR_HOURS = 4.0
TWO_HOURS = 2.0
TWENTY_HOURS = 20.0
TEN_HOURS = 10.0
WEIGHT_HALF = 0.5
WEIGHT_SIXTY = 0.6
WEIGHT_FORTY = 0.4
WEIGHT_FULL = 1.0
SHARE_SIXTY_PERCENT = 60.0
SHARE_FORTY_PERCENT = 40.0
EXPECTED_TWO_HOME_OFFICE_DAYS = 2
EXPECTED_ONE_DAY = 1
EXPECTED_FOUR_DAILY_ENTRIES = 4
# endregion


# region Fixtures
@pytest.fixture
def exporter_env(tmp_path: Path) -> tuple[Path, TelemetryRecorder]:
    """Provide an isolated SQLite database path and `TelemetryRecorder`."""
    db_path = tmp_path / "exporter_node.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    return db_path, telemetry


def _seed_workplace_history(ledger: Ledger) -> None:
    """Seed 4 days of intervals and date/interval workplace `ContextFacts`."""
    activity = ledger.create_activity("Core Engineering", external_ref="ENG-1")

    # Day 1: 8h home_office via sensor date fact
    ledger.record_structured_interval(
        activity.id,
        started_at=datetime(2026, 11, 2, 9, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 2, 17, 0, 0, tzinfo=UTC),
    )
    ledger.assert_context_fact("workplace", "home_office", scope_date=DAY_ONE, source="sensor")

    # Day 2: 6h office via manual date fact
    ledger.record_structured_interval(
        activity.id,
        started_at=datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 3, 16, 0, 0, tzinfo=UTC),
    )
    ledger.assert_context_fact("workplace", "office", scope_date=DAY_TWO, source="manual")

    # Day 3: 4h home_office via interval-scoped fact
    day_three_interval = ledger.record_structured_interval(
        activity.id,
        started_at=datetime(2026, 11, 4, 8, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC),
    )
    ledger.assert_context_fact(
        "workplace",
        "home_office",
        interval_id=day_three_interval.id,
        source="sensor",
    )

    # Day 4: 2h tracked with no workplace ContextFact -> unclassified
    ledger.record_structured_interval(
        activity.id,
        started_at=datetime(2026, 11, 5, 14, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 11, 5, 16, 0, 0, tzinfo=UTC),
    )


def _seed_multi_entity_dag(ledger: Ledger) -> None:
    """Seed multi-entity DAG (`Activity -> Component -> Project -> Customer/Epic/CostCenter`)."""
    act = ledger.create_activity("Platform Feature", external_ref="PLAT-42")
    comp = ledger.create_entity(ENTITY_KIND_COMPONENT, "auth-svc", "Auth Service")
    proj = ledger.create_entity(ENTITY_KIND_PROJECT, "sec-2026", "Security 2026")
    epic = ledger.create_entity(ENTITY_KIND_EPIC, "EPIC-99", "Zero Trust Epic")
    cost_center = ledger.create_entity(ENTITY_KIND_COST_CENTER, "CC-4100", "R&D Security")
    cust_acme = ledger.create_entity(ENTITY_KIND_CUSTOMER, "acme", "Acme Corp")
    cust_globex = ledger.create_entity(ENTITY_KIND_CUSTOMER, "globex", "Globex Ltd")

    ledger.set_attribution(source_activity_id=act.id, target_entity_id=comp.id, weight=WEIGHT_FULL)
    ledger.set_attribution(source_activity_id=act.id, target_entity_id=epic.id, weight=WEIGHT_FULL)
    ledger.set_attribution(source_entity_id=comp.id, target_entity_id=proj.id, weight=WEIGHT_FULL)
    ledger.set_attribution(
        source_entity_id=proj.id, target_entity_id=cost_center.id, weight=WEIGHT_HALF
    )
    ledger.set_attribution(
        source_entity_id=proj.id, target_entity_id=cust_acme.id, weight=WEIGHT_SIXTY
    )
    ledger.set_attribution(
        source_entity_id=proj.id, target_entity_id=cust_globex.id, weight=WEIGHT_FORTY
    )

    # 10 hours total across two intervals inside [WINDOW_START, WINDOW_END)
    ledger.record_structured_interval(
        act.id,
        started_at=WINDOW_START + timedelta(hours=9),
        ended_at=WINDOW_START + timedelta(hours=15),
    )
    ledger.record_structured_interval(
        act.id,
        started_at=WINDOW_START + timedelta(days=1, hours=10),
        ended_at=WINDOW_START + timedelta(days=1, hours=14),
    )


# endregion


# region Workplace Tax Export Tests
def test_workplace_tax_report_aggregates_days_hours_and_provenance(
    exporter_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify `WorkplaceTaxReport` computes daily breakdown, provenance, and workplace totals."""
    db_path, telemetry = exporter_env
    with Ledger(db_path, telemetry=telemetry) as ledger:
        _seed_workplace_history(ledger)
        exporter = TaxAndAttributionExporter(ledger=ledger, telemetry=telemetry)

        report = exporter.build_workplace_tax_report(start_date=DAY_ONE, end_date=DAY_FOUR)
        assert isinstance(report, WorkplaceTaxReport)
        assert report.start_date == DAY_ONE.isoformat()
        assert report.end_date == DAY_FOUR.isoformat()
        assert report.days_by_workplace["home_office"] == EXPECTED_TWO_HOME_OFFICE_DAYS
        assert report.days_by_workplace["office"] == EXPECTED_ONE_DAY
        assert report.days_by_workplace["mobile_work"] == 0
        assert report.days_by_workplace[WORKPLACE_UNCLASSIFIED] == EXPECTED_ONE_DAY

        assert report.hours_by_workplace["home_office"] == pytest.approx(EIGHT_HOURS + FOUR_HOURS)
        assert report.hours_by_workplace["office"] == pytest.approx(SIX_HOURS)
        assert report.hours_by_workplace[WORKPLACE_UNCLASSIFIED] == pytest.approx(TWO_HOURS)
        assert report.total_tracked_hours == pytest.approx(TWENTY_HOURS)
        assert len(report.daily_entries) == EXPECTED_FOUR_DAILY_ENTRIES
        assert all(isinstance(entry, WorkplaceDayEntry) for entry in report.daily_entries)
        assert all(isinstance(row, WorkplaceSummaryRow) for row in report.summary_rows)

        annual = exporter.build_workplace_tax_report(year=TAX_YEAR)
        assert annual.start_date == f"{TAX_YEAR}-01-01"
        assert annual.end_date == f"{TAX_YEAR}-12-31"
        assert annual.total_tracked_hours == pytest.approx(TWENTY_HOURS)


def test_workplace_tax_report_renders_deterministic_json_csv_and_markdown(
    exporter_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify deterministic CSV, JSON, and Markdown rendering for `WorkplaceTaxReport`."""
    db_path, telemetry = exporter_env
    with Ledger(db_path, telemetry=telemetry) as ledger:
        _seed_workplace_history(ledger)
        exporter = TaxAndAttributionExporter(ledger=ledger, telemetry=telemetry)

        json_out = exporter.export_workplace_tax_report(
            start_date=DAY_ONE, end_date=DAY_FOUR, fmt=ExportFormat.JSON
        )
        parsed_json = json.loads(json_out)
        assert parsed_json["days_by_workplace"]["home_office"] == EXPECTED_TWO_HOME_OFFICE_DAYS
        assert parsed_json["hours_by_workplace"]["office"] == pytest.approx(SIX_HOURS)

        csv_out = exporter.export_workplace_tax_report(
            start_date=DAY_ONE, end_date=DAY_FOUR, fmt=ExportFormat.CSV
        )
        csv_rows = list(csv.DictReader(io.StringIO(csv_out)))
        summary_home = next(
            row
            for row in csv_rows
            if row["record_type"] == "summary" and row["workplace"] == "home_office"
        )
        assert int(summary_home["day_count"]) == EXPECTED_TWO_HOME_OFFICE_DAYS
        assert float(summary_home["tracked_hours"]) == pytest.approx(EIGHT_HOURS + FOUR_HOURS)
        assert summary_home["source"] == "sensor"

        md_out = exporter.export_workplace_tax_report(
            start_date=DAY_ONE, end_date=DAY_FOUR, fmt="markdown"
        )
        assert "# Workplace Tax-Return Summary" in md_out
        assert "`home_office`" in md_out
        assert "`unclassified`" in md_out
        assert DAY_ONE.isoformat() in md_out


# endregion


# region Entity Attribution Export Tests
def test_entity_attribution_report_propagates_weights_and_records_telemetry(
    exporter_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify multi-entity transitive DAG attribution report and Prometheus counter."""
    db_path, telemetry = exporter_env
    with Ledger(db_path, telemetry=telemetry) as ledger:
        _seed_multi_entity_dag(ledger)
        exporter = TaxAndAttributionExporter(ledger=ledger, telemetry=telemetry)

        report = exporter.build_entity_attribution_report(since=WINDOW_START, until=WINDOW_END)
        assert isinstance(report, EntityAttributionReport)
        assert report.total_interval_hours == pytest.approx(TEN_HOURS)
        assert all(isinstance(row, EntityStatementRow) for row in report.rows)

        by_key = {(row.kind, row.key): row for row in report.rows}
        assert by_key[(ENTITY_KIND_COMPONENT, "auth-svc")].attributed_hours == pytest.approx(
            TEN_HOURS
        )
        assert by_key[(ENTITY_KIND_PROJECT, "sec-2026")].attributed_hours == pytest.approx(
            TEN_HOURS
        )
        assert by_key[(ENTITY_KIND_EPIC, "EPIC-99")].attributed_hours == pytest.approx(TEN_HOURS)
        assert by_key[(ENTITY_KIND_COST_CENTER, "CC-4100")].attributed_hours == pytest.approx(
            TEN_HOURS * WEIGHT_HALF
        )
        assert by_key[(ENTITY_KIND_CUSTOMER, "acme")].attributed_hours == pytest.approx(SIX_HOURS)
        assert by_key[(ENTITY_KIND_CUSTOMER, "acme")].percentage_of_kind == pytest.approx(
            SHARE_SIXTY_PERCENT
        )
        assert by_key[(ENTITY_KIND_CUSTOMER, "globex")].attributed_hours == pytest.approx(
            FOUR_HOURS
        )
        assert by_key[(ENTITY_KIND_CUSTOMER, "globex")].percentage_of_kind == pytest.approx(
            SHARE_FORTY_PERCENT
        )

        metrics_text = telemetry.render_metrics_text()
        assert "worktrack_attribution_queries_total 1.0" in metrics_text


def test_entity_attribution_report_renders_csv_json_markdown_and_validates(
    exporter_env: tuple[Path, TelemetryRecorder],
) -> None:
    """Verify CSV, JSON, and Markdown rendering for `EntityAttributionReport` and validation."""
    db_path, telemetry = exporter_env
    with Ledger(db_path, telemetry=telemetry) as ledger:
        _seed_multi_entity_dag(ledger)
        exporter = TaxAndAttributionExporter(ledger=ledger, telemetry=telemetry)

        json_text = exporter.export_entity_attribution_report(
            since=WINDOW_START,
            until=WINDOW_END,
            target_kind=ENTITY_KIND_CUSTOMER,
            fmt=ExportFormat.JSON,
        )
        parsed = json.loads(json_text)
        assert len(parsed["rows"]) == 2
        assert parsed["rows"][0]["key"] == "acme"

        csv_text = exporter.export_entity_attribution_report(
            since=WINDOW_START,
            until=WINDOW_END,
            target_kind=ENTITY_KIND_CUSTOMER,
            fmt=ExportFormat.CSV,
        )
        csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
        assert [row["key"] for row in csv_rows] == ["acme", "globex"]

        md_text = exporter.export_entity_attribution_report(
            since=WINDOW_START,
            until=WINDOW_END,
            fmt=ExportFormat.MARKDOWN,
        )
        assert "# Multi-Entity Attribution Statement" in md_text
        assert "`acme`" in md_text
        assert "`CC-4100`" in md_text

        report_obj = exporter.build_entity_attribution_report(since=WINDOW_START, until=WINDOW_END)
        assert report_obj.render("md") == md_text
        assert report_obj.to_csv() == exporter.export_entity_attribution_report(
            since=WINDOW_START, until=WINDOW_END, fmt=ExportFormat.CSV
        )

        wp_report = exporter.build_workplace_tax_report(start_date=DAY_ONE, end_date=DAY_FOUR)
        assert wp_report.render("md") == wp_report.to_markdown()
        assert wp_report.render(ExportFormat.CSV) == wp_report.to_csv()

        with pytest.raises(ExporterValidationError):
            exporter.export_workplace_tax_report(
                start_date=DAY_FOUR, end_date=DAY_ONE, fmt=ExportFormat.JSON
            )
        with pytest.raises(ExporterValidationError):
            exporter.export_entity_attribution_report(fmt="xml")


# endregion
