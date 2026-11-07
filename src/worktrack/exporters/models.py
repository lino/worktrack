"""Canonical models, formats, and exceptions for `worktrack.exporters`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from worktrack.core.exceptions import LedgerError, LedgerValidationError

# region Constants
EXPORT_FORMAT_CSV: Final = "csv"
EXPORT_FORMAT_JSON: Final = "json"
EXPORT_FORMAT_MARKDOWN: Final = "markdown"
MARKDOWN_SHORT_ALIAS: Final = "md"
ALLOWED_EXPORT_FORMATS: Final[frozenset[str]] = frozenset(
    {EXPORT_FORMAT_CSV, EXPORT_FORMAT_JSON, EXPORT_FORMAT_MARKDOWN}
)

WORKPLACE_UNCLASSIFIED: Final = "unclassified"
PROVENANCE_NONE: Final = "none"
SECONDS_PER_HOUR: Final = 3600.0
PERCENT_MULTIPLIER: Final = 100.0
HOURS_PRECISION: Final = 4
PERCENT_PRECISION: Final = 2
JSON_INDENT_SPACES: Final = 2
# endregion


# region Exceptions
class ExporterError(LedgerError):
    """Base exception for `worktrack.exporters` report calculation and rendering."""


class ExporterValidationError(ExporterError, LedgerValidationError):
    """Raised when exporter date bounds, formats, or filters are invalid."""


# endregion


# region Models
class ExportFormat(StrEnum):
    """Supported deterministic export serialization formats."""

    CSV = EXPORT_FORMAT_CSV
    JSON = EXPORT_FORMAT_JSON
    MARKDOWN = EXPORT_FORMAT_MARKDOWN


@dataclass(frozen=True, slots=True)
class WorkplaceDayEntry:
    """Daily `workplace` classification, tracked duration, and assertion provenance."""

    date_iso: str
    workplace: str
    tracked_seconds: float
    tracked_hours: float
    interval_count: int
    source: str

    def to_dict(self) -> dict[str, object]:
        """Serialize daily entry into a JSON-compatible dictionary."""
        return {
            "date_iso": self.date_iso,
            "workplace": self.workplace,
            "tracked_seconds": self.tracked_seconds,
            "tracked_hours": self.tracked_hours,
            "interval_count": self.interval_count,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class WorkplaceSummaryRow:
    """Aggregated calendar days, tracked duration, and provenance per `workplace`."""

    workplace: str
    day_count: int
    tracked_seconds: float
    tracked_hours: float
    sources: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Serialize summary row into a JSON-compatible dictionary."""
        return {
            "workplace": self.workplace,
            "day_count": self.day_count,
            "tracked_seconds": self.tracked_seconds,
            "tracked_hours": self.tracked_hours,
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class WorkplaceTaxReport:
    """Annual or custom-window `workplace` tax-return summary report."""

    start_date: str
    end_date: str
    summary_rows: tuple[WorkplaceSummaryRow, ...]
    daily_entries: tuple[WorkplaceDayEntry, ...]
    days_by_workplace: dict[str, int]
    hours_by_workplace: dict[str, float]
    seconds_by_workplace: dict[str, float]
    total_classified_days: int
    total_unclassified_days: int
    total_tracked_seconds: float
    total_tracked_hours: float

    def to_dict(self) -> dict[str, object]:
        """Serialize `WorkplaceTaxReport` into a deterministic JSON dictionary."""
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total_classified_days": self.total_classified_days,
            "total_unclassified_days": self.total_unclassified_days,
            "total_tracked_seconds": self.total_tracked_seconds,
            "total_tracked_hours": self.total_tracked_hours,
            "days_by_workplace": dict(self.days_by_workplace),
            "hours_by_workplace": dict(self.hours_by_workplace),
            "seconds_by_workplace": dict(self.seconds_by_workplace),
            "summary_rows": [row.to_dict() for row in self.summary_rows],
            "daily_entries": [entry.to_dict() for entry in self.daily_entries],
        }

    def to_json(self) -> str:
        """Render `WorkplaceTaxReport` as deterministic pretty-printed JSON."""
        return json.dumps(self.to_dict(), indent=JSON_INDENT_SPACES, sort_keys=True) + "\n"

    def to_csv(self) -> str:
        """Render `WorkplaceTaxReport` as deterministic LF-terminated CSV."""
        from worktrack.exporters.render import render_workplace_csv

        return render_workplace_csv(self)

    def to_markdown(self) -> str:
        """Render `WorkplaceTaxReport` as a GitHub-Flavored Markdown report."""
        from worktrack.exporters.render import render_workplace_markdown

        return render_workplace_markdown(self)

    def render(self, fmt: ExportFormat | str = ExportFormat.JSON) -> str:
        """Render `WorkplaceTaxReport` in the requested `ExportFormat`."""
        from worktrack.exporters.render import render_workplace_tax_report

        return render_workplace_tax_report(self, fmt)


@dataclass(frozen=True, slots=True)
class EntityStatementRow:
    """Attributed duration, hours, and relative kind share for a single `Entity`."""

    entity_id: str
    kind: str
    key: str
    name: str
    attributed_seconds: float
    attributed_hours: float
    share_of_kind: float
    percentage_of_kind: float

    def to_dict(self) -> dict[str, object]:
        """Serialize `EntityStatementRow` into a JSON-compatible dictionary."""
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "key": self.key,
            "name": self.name,
            "attributed_seconds": self.attributed_seconds,
            "attributed_hours": self.attributed_hours,
            "share_of_kind": self.share_of_kind,
            "percentage_of_kind": self.percentage_of_kind,
        }


@dataclass(frozen=True, slots=True)
class EntityAttributionReport:
    """Multi-entity transitive weighted attribution statement over `[window_start, window_end)`."""

    window_start: str | None
    window_end: str | None
    target_kind: str | None
    normalized: bool
    total_interval_seconds: float
    total_interval_hours: float
    rows: tuple[EntityStatementRow, ...]
    totals_by_kind: dict[str, float]
    hours_by_kind: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        """Serialize `EntityAttributionReport` into a deterministic JSON dictionary."""
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "target_kind": self.target_kind,
            "normalized": self.normalized,
            "total_interval_seconds": self.total_interval_seconds,
            "total_interval_hours": self.total_interval_hours,
            "totals_by_kind": dict(self.totals_by_kind),
            "hours_by_kind": dict(self.hours_by_kind),
            "rows": [row.to_dict() for row in self.rows],
        }

    def to_json(self) -> str:
        """Render `EntityAttributionReport` as deterministic pretty-printed JSON."""
        return json.dumps(self.to_dict(), indent=JSON_INDENT_SPACES, sort_keys=True) + "\n"

    def to_csv(self) -> str:
        """Render `EntityAttributionReport` as deterministic LF-terminated CSV."""
        from worktrack.exporters.render import render_entity_attribution_csv

        return render_entity_attribution_csv(self)

    def to_markdown(self) -> str:
        """Render `EntityAttributionReport` as a GitHub-Flavored Markdown statement."""
        from worktrack.exporters.render import render_entity_attribution_markdown

        return render_entity_attribution_markdown(self)

    def render(self, fmt: ExportFormat | str = ExportFormat.JSON) -> str:
        """Render `EntityAttributionReport` in the requested `ExportFormat`."""
        from worktrack.exporters.render import render_entity_attribution_report

        return render_entity_attribution_report(self, fmt)


# endregion


# region Validation
def normalize_export_format(fmt: ExportFormat | str) -> ExportFormat:
    """Validate and normalize an export format identifier (`csv`, `json`, `markdown`)."""
    if isinstance(fmt, ExportFormat):
        return fmt
    if not isinstance(fmt, str) or not fmt.strip():
        raise ExporterValidationError("Export format must be a non-empty string.")
    cleaned = fmt.strip().lower()
    if cleaned == MARKDOWN_SHORT_ALIAS:
        cleaned = EXPORT_FORMAT_MARKDOWN
    if cleaned not in ALLOWED_EXPORT_FORMATS:
        allowed = ", ".join(sorted(ALLOWED_EXPORT_FORMATS))
        raise ExporterValidationError(f"Unsupported export format '{fmt}'; expected: {allowed}.")
    return ExportFormat(cleaned)


# endregion
