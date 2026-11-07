"""Deterministic CSV, JSON, and Markdown formatters for tax and attribution reports."""

from __future__ import annotations

import csv
import io
from typing import Final

from worktrack.exporters.models import (
    EntityAttributionReport,
    ExportFormat,
    WorkplaceTaxReport,
    normalize_export_format,
)

# region Constants
CSV_LINETERMINATOR: Final = "\n"
SOURCE_SEPARATOR: Final = ";"
RECORD_TYPE_SUMMARY: Final = "summary"
RECORD_TYPE_DAILY: Final = "daily"
SINGLE_CALENDAR_DAY: Final = 1
UNBOUNDED_WINDOW_LABEL: Final = "all_time"
# endregion


# region Rendering
def render_workplace_tax_report(
    report: WorkplaceTaxReport,
    fmt: ExportFormat | str = ExportFormat.JSON,
) -> str:
    """Render a `WorkplaceTaxReport` as `JSON`, `CSV`, or `Markdown`."""
    resolved = normalize_export_format(fmt)
    if resolved == ExportFormat.JSON:
        return report.to_json()
    if resolved == ExportFormat.CSV:
        return render_workplace_csv(report)
    return render_workplace_markdown(report)


def render_workplace_csv(report: WorkplaceTaxReport) -> str:
    """Render `WorkplaceTaxReport` summary and daily rows as deterministic CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator=CSV_LINETERMINATOR)
    writer.writerow(
        [
            "record_type",
            "workplace",
            "date_iso",
            "day_count",
            "tracked_seconds",
            "tracked_hours",
            "interval_count",
            "source",
        ]
    )
    for summary in report.summary_rows:
        writer.writerow(
            [
                RECORD_TYPE_SUMMARY,
                summary.workplace,
                "",
                summary.day_count,
                f"{summary.tracked_seconds:.2f}",
                f"{summary.tracked_hours:.4f}",
                "",
                SOURCE_SEPARATOR.join(summary.sources),
            ]
        )
    for entry in report.daily_entries:
        writer.writerow(
            [
                RECORD_TYPE_DAILY,
                entry.workplace,
                entry.date_iso,
                SINGLE_CALENDAR_DAY,
                f"{entry.tracked_seconds:.2f}",
                f"{entry.tracked_hours:.4f}",
                entry.interval_count,
                entry.source,
            ]
        )
    return buf.getvalue()


def render_workplace_markdown(report: WorkplaceTaxReport) -> str:
    """Render `WorkplaceTaxReport` as a GitHub-Flavored Markdown document."""
    lines = [
        f"# Workplace Tax-Return Summary ({report.start_date} .. {report.end_date})",
        "",
        f"- **Classified Days**: {report.total_classified_days}",
        f"- **Unclassified Days**: {report.total_unclassified_days}",
        f"- **Total Tracked Hours**: {report.total_tracked_hours:.2f}h "
        f"({report.total_tracked_seconds:.0f}s)",
        "",
        "## Summary by Workplace",
        "",
        "| Workplace | Days | Hours | Seconds | Provenance |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in report.summary_rows:
        prov = ", ".join(row.sources) if row.sources else "-"
        lines.append(
            f"| `{row.workplace}` | {row.day_count} | {row.tracked_hours:.2f} | "
            f"{row.tracked_seconds:.0f} | {prov} |"
        )
    lines.extend(
        [
            "",
            "## Daily Breakdown",
            "",
            "| Date | Workplace | Hours | Seconds | Intervals | Provenance |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for entry in report.daily_entries:
        lines.append(
            f"| {entry.date_iso} | `{entry.workplace}` | {entry.tracked_hours:.2f} | "
            f"{entry.tracked_seconds:.0f} | {entry.interval_count} | {entry.source} |"
        )
    return "\n".join(lines) + "\n"


def render_entity_attribution_report(
    report: EntityAttributionReport,
    fmt: ExportFormat | str = ExportFormat.JSON,
) -> str:
    """Render an `EntityAttributionReport` as `JSON`, `CSV`, or `Markdown`."""
    resolved = normalize_export_format(fmt)
    if resolved == ExportFormat.JSON:
        return report.to_json()
    if resolved == ExportFormat.CSV:
        return render_entity_attribution_csv(report)
    return render_entity_attribution_markdown(report)


def render_entity_attribution_csv(report: EntityAttributionReport) -> str:
    """Render `EntityAttributionReport` statement rows as deterministic CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator=CSV_LINETERMINATOR)
    writer.writerow(
        [
            "entity_id",
            "kind",
            "key",
            "name",
            "attributed_seconds",
            "attributed_hours",
            "share_of_kind",
            "percentage_of_kind",
        ]
    )
    for row in report.rows:
        writer.writerow(
            [
                row.entity_id,
                row.kind,
                row.key,
                row.name,
                f"{row.attributed_seconds:.2f}",
                f"{row.attributed_hours:.4f}",
                f"{row.share_of_kind:.6f}",
                f"{row.percentage_of_kind:.2f}",
            ]
        )
    return buf.getvalue()


def render_entity_attribution_markdown(report: EntityAttributionReport) -> str:
    """Render `EntityAttributionReport` as a GitHub-Flavored Markdown document."""
    start_lbl = report.window_start or UNBOUNDED_WINDOW_LABEL
    end_lbl = report.window_end or UNBOUNDED_WINDOW_LABEL
    lines = [
        f"# Multi-Entity Attribution Statement ({start_lbl} .. {end_lbl})",
        "",
        f"- **Total Interval Hours**: {report.total_interval_hours:.2f}h "
        f"({report.total_interval_seconds:.0f}s)",
        f"- **Normalized Outgoing Weights**: {report.normalized}",
        "",
        "| Kind | Key | Name | Hours | Seconds | Share (%) |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for row in report.rows:
        lines.append(
            f"| `{row.kind}` | `{row.key}` | {row.name} | {row.attributed_hours:.2f} | "
            f"{row.attributed_seconds:.0f} | {row.percentage_of_kind:.2f}% |"
        )
    return "\n".join(lines) + "\n"


# endregion
