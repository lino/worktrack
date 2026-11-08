"""Report serialization and machine-readable JSON Schema and CSVW schema exporters."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from worktrack.reporting.engine import JurisdictionReportSummary


class _CsvWriterProtocol(Protocol):
    """Minimal structural protocol for `csv.writer` instances."""

    def writerow(self, row: Iterable[object]) -> object:
        """Write a single CSV row."""

# region Constants
JSON_SCHEMA_DRAFT_2020_12: Final = "https://json-schema.org/draft/2020-12/schema"
CSVW_CONTEXT_URI: Final = "http://www.w3.org/ns/csvw"
CSV_COLUMNS: Final[tuple[str, ...]] = ("section", "item", "metric", "value", "unit")
# endregion


# region Schemas
def _pack_schema_definition() -> dict[str, object]:
    """Define schema constraints for ReportingPack metadata."""
    return {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "title": {"type": "string"},
            "currency": {"type": "string"},
            "tax_rules_enabled": {"type": "boolean"},
        },
        "required": ["slug", "title", "currency", "tax_rules_enabled"],
    }


def _workplace_metric_properties() -> dict[str, object]:
    """Define workplace day count and commute allowance schema properties."""
    return {
        "home_office_days_raw": {"type": "integer", "minimum": 0},
        "home_office_days_eligible": {"type": "integer", "minimum": 0},
        "home_office_days_capped": {"type": "integer", "minimum": 0},
        "home_office_allowance": {"type": "number", "minimum": 0.0},
        "office_days_raw": {"type": "integer", "minimum": 0},
        "office_days_eligible": {"type": "integer", "minimum": 0},
        "commute_daily_allowance": {"type": "number", "minimum": 0.0},
        "commute_allowance_total": {"type": "number", "minimum": 0.0},
        "mobile_work_days": {"type": "integer", "minimum": 0},
        "client_site_days": {"type": "integer", "minimum": 0},
    }


def _travel_metric_properties() -> dict[str, object]:
    """Define travel per-diem and mileage allowance schema properties."""
    return {
        "excluded_absence_or_holiday_dates": {
            "type": "array",
            "items": {"type": "string"},
        },
        "excluded_travel_dates": {
            "type": "array",
            "items": {"type": "string"},
        },
        "trips": {"type": "array", "items": {"type": "object"}},
        "travel_per_diem_total": {"type": "number", "minimum": 0.0},
        "travel_mileage_total": {"type": "number", "minimum": 0.0},
        "travel_allowance_total": {"type": "number", "minimum": 0.0},
        "absence_summary": {"type": ["object", "null"]},
    }


def reporting_summary_json_schema() -> dict[str, object]:
    """Produce machine-readable JSON Schema Draft 2020-12 for jurisdiction reports."""
    props: dict[str, object] = {
        "pack": _pack_schema_definition(),
        "start_date": {"type": "string"},
        "end_date": {"type": "string"},
        "office_one_way_km": {"type": "number", "minimum": 0.0},
    }
    props.update(_workplace_metric_properties())
    props.update(_travel_metric_properties())
    props["total_allowance_and_reimbursement"] = {"type": "number", "minimum": 0.0}

    return {
        "$schema": JSON_SCHEMA_DRAFT_2020_12,
        "title": "WorktrackJurisdictionReportSummary",
        "description": "Annual or custom-window jurisdiction workplace and travel report.",
        "type": "object",
        "properties": props,
        "required": [
            "pack",
            "start_date",
            "end_date",
            "home_office_allowance",
            "commute_allowance_total",
            "total_allowance_and_reimbursement",
        ],
    }


def reporting_summary_csvw_schema() -> dict[str, object]:
    """Produce machine-readable W3C CSV on the Web (CSVW) metadata schema."""
    cols = [
        {"name": "section", "datatype": "string", "dc:description": "Report category"},
        {"name": "item", "datatype": "string", "dc:description": "Line item identifier"},
        {"name": "metric", "datatype": "string", "dc:description": "Metric or property key"},
        {"name": "value", "datatype": "string", "dc:description": "Recorded value or amount"},
        {"name": "unit", "datatype": "string", "dc:description": "Measurement unit"},
    ]
    return {
        "@context": CSVW_CONTEXT_URI,
        "dc:title": "Worktrack Jurisdiction Reporting Summary",
        "dc:description": "Statutory workplace and travel allowance export metadata",
        "tableSchema": {
            "columns": cols,
            "primaryKey": ["section", "item", "metric"],
        },
    }


# endregion


# region Exporters
def export_report_json(summary: JurisdictionReportSummary) -> str:
    """Export jurisdiction report summary as pretty-printed JSON."""
    return json.dumps(summary.to_dict(), indent=2)


def export_report_csv(summary: JurisdictionReportSummary) -> str:
    """Export jurisdiction report summary as structured W3C-compliant CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)

    _write_metadata_csv_rows(writer, summary)
    _write_workplace_csv_rows(writer, summary)
    _write_travel_csv_rows(writer, summary)

    writer.writerow(
        [
            "totals",
            "grand_total",
            "total_allowance_and_reimbursement",
            f"{summary.total_allowance_and_reimbursement:.2f}",
            summary.pack.currency,
        ]
    )
    return buf.getvalue()


def _write_metadata_csv_rows(
    writer: _CsvWriterProtocol, summary: JurisdictionReportSummary
) -> None:
    """Write metadata rows to CSV stream."""
    curr = summary.pack.currency
    writer.writerow(["metadata", "jurisdiction", "slug", summary.pack.slug, ""])
    writer.writerow(["metadata", "jurisdiction", "title", summary.pack.title, ""])
    writer.writerow(["metadata", "window", "start_date", summary.start_date, ""])
    writer.writerow(["metadata", "window", "end_date", summary.end_date, ""])
    writer.writerow(["metadata", "currency", "code", curr, ""])


def _write_workplace_csv_rows(
    writer: _CsvWriterProtocol, summary: JurisdictionReportSummary
) -> None:
    """Write workplace day counts and allowances to CSV stream."""
    curr = summary.pack.currency
    p = summary.pack
    ho_raw = str(summary.home_office_days_raw)
    ho_elig = str(summary.home_office_days_eligible)
    ho_cap = str(summary.home_office_days_capped)
    ho_rate = f"{p.home_office_daily_rate:.2f}"
    ho_tot = f"{summary.home_office_allowance:.2f}"
    off_elig = str(summary.office_days_eligible)
    comm_rate = f"{summary.commute_daily_allowance:.2f}"
    comm_tot = f"{summary.commute_allowance_total:.2f}"

    writer.writerow(["workplace", "home_office", "raw_days", ho_raw, "days"])
    writer.writerow(["workplace", "home_office", "eligible_days", ho_elig, "days"])
    writer.writerow(["workplace", "home_office", "capped_days", ho_cap, "days"])
    writer.writerow(["workplace", "home_office", "daily_rate", ho_rate, curr])
    writer.writerow(["workplace", "home_office", "allowance_total", ho_tot, curr])
    writer.writerow(["workplace", "office", "raw_days", str(summary.office_days_raw), "days"])
    writer.writerow(["workplace", "office", "eligible_days", off_elig, "days"])
    writer.writerow(["workplace", "office", "one_way_km", f"{summary.office_one_way_km:.1f}", "km"])
    writer.writerow(["workplace", "office", "daily_commute_allowance", comm_rate, curr])
    writer.writerow(["workplace", "office", "commute_allowance_total", comm_tot, curr])
    writer.writerow(["workplace", "mobile_work", "days", str(summary.mobile_work_days), "days"])
    writer.writerow(["workplace", "client_site", "days", str(summary.client_site_days), "days"])


def _write_travel_csv_rows(
    writer: _CsvWriterProtocol, summary: JurisdictionReportSummary
) -> None:
    """Write business travel totals and trip rows to CSV stream."""
    curr = summary.pack.currency
    p_tot = f"{summary.travel_per_diem_total:.2f}"
    m_tot = f"{summary.travel_mileage_total:.2f}"
    t_tot = f"{summary.travel_allowance_total:.2f}"
    writer.writerow(["travel", "totals", "per_diem_total", p_tot, curr])
    writer.writerow(["travel", "totals", "mileage_total", m_tot, curr])
    writer.writerow(["travel", "totals", "travel_allowance_total", t_tot, curr])
    for trip_calc in summary.trips:
        t_id = trip_calc.trip.id
        writer.writerow(["trip", t_id, "title", trip_calc.trip.title, ""])
        writer.writerow(["trip", t_id, "destination", trip_calc.trip.destination, ""])
        writer.writerow(
            ["trip", t_id, "net_per_diem", f"{trip_calc.net_per_diem_total:.2f}", curr]
        )
        writer.writerow(
            ["trip", t_id, "mileage_allowance", f"{trip_calc.mileage_allowance:.2f}", curr]
        )
        writer.writerow(
            ["trip", t_id, "total_reimbursement", f"{trip_calc.total_reimbursement:.2f}", curr]
        )


def _render_workplace_markdown_table(
    summary: JurisdictionReportSummary, curr: str
) -> list[str]:
    """Render workplace category summary table for Markdown output."""
    p = summary.pack
    ho_line = (
        f"| Home Office Allowance | {summary.home_office_days_raw} | "
        f"{summary.home_office_days_eligible} (capped: {summary.home_office_days_capped}) | "
        f"{p.home_office_daily_rate:.2f} {curr} | "
        f"{summary.home_office_allowance:.2f} {curr} |"
    )
    off_line = (
        f"| Office Commute | {summary.office_days_raw} | {summary.office_days_eligible} | "
        f"{summary.commute_daily_allowance:.2f} {curr} ({summary.office_one_way_km:.1f} km) | "
        f"{summary.commute_allowance_total:.2f} {curr} |"
    )
    mob_line = (
        f"| Mobile Work | {summary.mobile_work_days} | {summary.mobile_work_days} | "
        f"0.00 {curr} | 0.00 {curr} |"
    )
    cli_line = (
        f"| Client Site | {summary.client_site_days} | {summary.client_site_days} | "
        f"0.00 {curr} | 0.00 {curr} |"
    )
    return [
        "## Workplace Summary & Allowances",
        "",
        "| Workplace Category | Tracked Days | Eligible Days | Rate / Day | Total Allowance |",
        "| :--- | :--- | :--- | :--- | :--- |",
        ho_line,
        off_line,
        mob_line,
        cli_line,
    ]


def _render_exclusions_markdown(summary: JurisdictionReportSummary) -> list[str]:
    """Render excluded holiday and travel dates notes for Markdown export."""
    lines: list[str] = []
    if summary.excluded_absence_or_holiday_dates:
        excluded_str = ", ".join(summary.excluded_absence_or_holiday_dates)
        lines.extend(["", "### Excluded Absence and Holiday Dates", f"- {excluded_str}"])
    if summary.excluded_travel_dates:
        travel_str = ", ".join(summary.excluded_travel_dates)
        lines.extend(["", "### Excluded Full Travel Dates", f"- {travel_str}"])
    return lines


def export_report_markdown(summary: JurisdictionReportSummary) -> str:
    """Export jurisdiction report summary as clean formatted Markdown document."""
    curr = summary.pack.currency
    tot_reimb = f"{summary.total_allowance_and_reimbursement:.2f} {curr}"
    header_lines = [
        f"# Jurisdiction Report: {summary.pack.title}",
        "",
        f"- **Reporting Pack**: `{summary.pack.slug}` ({summary.pack.title})",
        f"- **Reporting Period**: `{summary.start_date}` to `{summary.end_date}`",
        f"- **Currency**: `{curr}`",
        f"- **Total Statutory Allowance**: **`{tot_reimb}`**",
        "",
    ]
    travel_lines = [
        "",
        "## Business Travel & Reimbursements",
        "",
        f"- **Travel Per-Diem Total**: `{summary.travel_per_diem_total:.2f} {curr}`",
        f"- **Car Mileage Total**: `{summary.travel_mileage_total:.2f} {curr}`",
        f"- **Travel Reimbursement Total**: `{summary.travel_allowance_total:.2f} {curr}`",
        f"- **Trips Count**: `{len(summary.trips)}`",
    ]
    all_lines = (
        header_lines
        + _render_workplace_markdown_table(summary, curr)
        + travel_lines
        + _render_exclusions_markdown(summary)
        + [""]
    )
    return "\n".join(all_lines)


# endregion
