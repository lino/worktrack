"""`TaxAndAttributionExporter` for annual `workplace` tax and multi-entity attribution reports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final

from worktrack.core import Ledger
from worktrack.core.ledger_mixins import METRIC_ATTRIBUTION_QUERIES_TOTAL
from worktrack.core.models import (
    BUILTIN_WORKPLACE_FACET_KEY,
    BUILTIN_WORKPLACE_VALUES,
    ContextFact,
    Interval,
    ensure_utc,
    format_utc_iso,
    normalize_scope_date,
)
from worktrack.exporters.models import (
    HOURS_PRECISION,
    PERCENT_MULTIPLIER,
    PERCENT_PRECISION,
    PROVENANCE_NONE,
    SECONDS_PER_HOUR,
    WORKPLACE_UNCLASSIFIED,
    EntityAttributionReport,
    EntityStatementRow,
    ExporterValidationError,
    ExportFormat,
    WorkplaceDayEntry,
    WorkplaceSummaryRow,
    WorkplaceTaxReport,
)
from worktrack.exporters.render import (
    render_entity_attribution_report,
    render_workplace_tax_report,
)
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
JANUARY_FIRST_MONTH: Final = 1
JANUARY_FIRST_DAY: Final = 1
DECEMBER_LAST_MONTH: Final = 12
DECEMBER_LAST_DAY: Final = 31
SINGLE_DAY_DELTA: Final = 1
ZERO_FLOAT: Final = 0.0
ZERO_INT: Final = 0
# endregion


# region Tax Exporter
class TaxAndAttributionExporter:
    """Generate deterministic `workplace` tax-return and `Entity` attribution reports."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        """Bind exporter to `ledger` and optional `telemetry` recorder."""
        self._ledger = ledger
        self._telemetry = telemetry if telemetry is not None else ledger._telemetry

    def build_workplace_tax_report(
        self,
        *,
        start_date: date | str | None = None,
        end_date: date | str | None = None,
        year: int | None = None,
    ) -> WorkplaceTaxReport:
        """Compute `WorkplaceTaxReport` across `[start_date, end_date]` or calendar `year`."""
        resolved_start, resolved_end = _resolve_date_bounds(
            start_date=start_date, end_date=end_date, year=year
        )
        start_iso = resolved_start.isoformat()
        end_iso = resolved_end.isoformat()
        window_start = datetime(
            resolved_start.year, resolved_start.month, resolved_start.day, tzinfo=UTC
        )
        window_end = datetime(
            resolved_end.year, resolved_end.month, resolved_end.day, tzinfo=UTC
        ) + timedelta(days=SINGLE_DAY_DELTA)

        intervals = self._ledger.list_intervals(
            since=window_start, until=window_end, include_open=False
        )
        all_facts = self._ledger.list_context_facts(facet_key=BUILTIN_WORKPLACE_FACET_KEY)
        date_facts, interval_facts = _index_workplace_facts(all_facts, start_iso, end_iso)
        day_buckets = _accumulate_daily_intervals(intervals, window_start, window_end)
        daily_entries = _build_daily_workplace_entries(
            day_buckets=day_buckets,
            date_facts=date_facts,
            interval_facts=interval_facts,
        )
        return _assemble_workplace_report(start_iso, end_iso, daily_entries)

    def export_workplace_tax_report(
        self,
        *,
        start_date: date | str | None = None,
        end_date: date | str | None = None,
        year: int | None = None,
        fmt: ExportFormat | str = ExportFormat.JSON,
    ) -> str:
        """Compute and render a `WorkplaceTaxReport` as `CSV`, `JSON`, or `Markdown`."""
        report = self.build_workplace_tax_report(
            start_date=start_date, end_date=end_date, year=year
        )
        return render_workplace_tax_report(report, fmt)

    # endregion

    # region Attribution Exporter
    def build_entity_attribution_report(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        target_kind: str | None = None,
        normalize: bool = False,
    ) -> EntityAttributionReport:
        """Compute `EntityAttributionReport` across `[since, until)` via DAG propagation."""
        utc_since, utc_until = _validate_datetime_window(since, until)
        shares = self._ledger.query_entity_attribution(
            since=utc_since,
            until=utc_until,
            target_kind=target_kind,
            normalize=normalize,
        )
        if self._ledger._telemetry is None and self._telemetry is not None:
            self._telemetry.increment_counter(METRIC_ATTRIBUTION_QUERIES_TOTAL)

        total_interval_secs = _sum_clipped_intervals(self._ledger, utc_since, utc_until)
        rows = tuple(
            EntityStatementRow(
                entity_id=share.entity.id,
                kind=share.entity.kind,
                key=share.entity.key,
                name=share.entity.name,
                attributed_seconds=share.attributed_seconds,
                attributed_hours=round(
                    share.attributed_seconds / SECONDS_PER_HOUR, HOURS_PRECISION
                ),
                share_of_kind=share.share_of_kind,
                percentage_of_kind=round(
                    share.share_of_kind * PERCENT_MULTIPLIER, PERCENT_PRECISION
                ),
            )
            for share in shares
        )
        totals_by_kind: dict[str, float] = {}
        for row in rows:
            totals_by_kind[row.kind] = totals_by_kind.get(row.kind, ZERO_FLOAT) + (
                row.attributed_seconds
            )
        hours_by_kind = {
            kind: round(secs / SECONDS_PER_HOUR, HOURS_PRECISION)
            for kind, secs in totals_by_kind.items()
        }
        return EntityAttributionReport(
            window_start=None if utc_since is None else format_utc_iso(utc_since),
            window_end=None if utc_until is None else format_utc_iso(utc_until),
            target_kind=target_kind,
            normalized=normalize,
            total_interval_seconds=total_interval_secs,
            total_interval_hours=round(total_interval_secs / SECONDS_PER_HOUR, HOURS_PRECISION),
            rows=rows,
            totals_by_kind=totals_by_kind,
            hours_by_kind=hours_by_kind,
        )

    def export_entity_attribution_report(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        target_kind: str | None = None,
        normalize: bool = False,
        fmt: ExportFormat | str = ExportFormat.JSON,
    ) -> str:
        """Compute and render an `EntityAttributionReport` as `CSV`, `JSON`, or `Markdown`."""
        report = self.build_entity_attribution_report(
            since=since,
            until=until,
            target_kind=target_kind,
            normalize=normalize,
        )
        return render_entity_attribution_report(report, fmt)


def _resolve_date_bounds(
    *,
    start_date: date | str | None,
    end_date: date | str | None,
    year: int | None,
) -> tuple[date, date]:
    """Validate and resolve `[start_date, end_date]` calendar bounds."""
    if year is not None:
        if isinstance(year, bool) or not isinstance(year, int):
            raise ExporterValidationError("year must be an integer.")
        default_start = date(year, JANUARY_FIRST_MONTH, JANUARY_FIRST_DAY)
        default_end = date(year, DECEMBER_LAST_MONTH, DECEMBER_LAST_DAY)
    else:
        current_year = datetime.now(UTC).year
        default_start = date(current_year, JANUARY_FIRST_MONTH, JANUARY_FIRST_DAY)
        default_end = date(current_year, DECEMBER_LAST_MONTH, DECEMBER_LAST_DAY)

    try:
        resolved_start = (
            default_start
            if start_date is None
            else date.fromisoformat(normalize_scope_date(start_date))
        )
        resolved_end = (
            default_end if end_date is None else date.fromisoformat(normalize_scope_date(end_date))
        )
    except Exception as exc:
        raise ExporterValidationError(str(exc)) from exc

    if resolved_end < resolved_start:
        raise ExporterValidationError("end_date must be greater than or equal to start_date.")
    return resolved_start, resolved_end


def _index_workplace_facts(
    facts: Sequence[ContextFact],
    start_iso: str,
    end_iso: str,
) -> tuple[dict[str, ContextFact], dict[str, ContextFact]]:
    """Index date-scoped and interval-scoped `workplace` `ContextFacts`."""
    by_date: dict[str, ContextFact] = {}
    by_interval: dict[str, ContextFact] = {}
    for fact in facts:
        if fact.scope_date is not None and start_iso <= fact.scope_date <= end_iso:
            by_date[fact.scope_date] = fact
        elif fact.interval_id is not None:
            by_interval[fact.interval_id] = fact
    return by_date, by_interval


def _accumulate_daily_intervals(
    intervals: Sequence[Interval],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, list[tuple[Interval, float]]]:
    """Slice closed `Intervals` across UTC calendar day boundaries inside `[start, end)`."""
    buckets: dict[str, list[tuple[Interval, float]]] = {}
    for interval in intervals:
        if interval.ended_at is None:
            continue
        eff_start = max(interval.started_at, window_start)
        eff_end = min(interval.ended_at, window_end)
        cursor = eff_start
        while cursor < eff_end:
            day_start = datetime(cursor.year, cursor.month, cursor.day, tzinfo=UTC)
            day_end = day_start + timedelta(days=SINGLE_DAY_DELTA)
            slice_end = min(eff_end, day_end)
            secs = (slice_end - cursor).total_seconds()
            if secs > ZERO_FLOAT:
                buckets.setdefault(day_start.date().isoformat(), []).append((interval, secs))
            cursor = slice_end
    return buckets


def _build_daily_workplace_entries(
    *,
    day_buckets: Mapping[str, list[tuple[Interval, float]]],
    date_facts: Mapping[str, ContextFact],
    interval_facts: Mapping[str, ContextFact],
) -> tuple[WorkplaceDayEntry, ...]:
    """Build chronological `WorkplaceDayEntry` records for all active or asserted dates."""
    all_dates = sorted(set(day_buckets.keys()) | set(date_facts.keys()))
    entries: list[WorkplaceDayEntry] = []
    for day_iso in all_dates:
        slices = day_buckets.get(day_iso, [])
        tracked_secs = sum(secs for _, secs in slices)
        workplace, source = _resolve_day_workplace(day_iso, slices, date_facts, interval_facts)
        entries.append(
            WorkplaceDayEntry(
                date_iso=day_iso,
                workplace=workplace,
                tracked_seconds=tracked_secs,
                tracked_hours=round(tracked_secs / SECONDS_PER_HOUR, HOURS_PRECISION),
                interval_count=len(slices),
                source=source,
            )
        )
    return tuple(entries)


def _resolve_day_workplace(
    day_iso: str,
    slices: Sequence[tuple[Interval, float]],
    date_facts: Mapping[str, ContextFact],
    interval_facts: Mapping[str, ContextFact],
) -> tuple[str, str]:
    """Resolve `(workplace, provenance_source)` for a single UTC calendar day."""
    date_fact = date_facts.get(day_iso)
    if date_fact is not None:
        return str(date_fact.value), date_fact.source
    for interval, _ in slices:
        int_fact = interval_facts.get(interval.id)
        if int_fact is not None:
            return str(int_fact.value), int_fact.source
    return WORKPLACE_UNCLASSIFIED, PROVENANCE_NONE


def _assemble_workplace_report(
    start_iso: str,
    end_iso: str,
    daily_entries: tuple[WorkplaceDayEntry, ...],
) -> WorkplaceTaxReport:
    """Aggregate `WorkplaceDayEntry` rows into a complete `WorkplaceTaxReport`."""
    canonical_keys = (*BUILTIN_WORKPLACE_VALUES, WORKPLACE_UNCLASSIFIED)
    days_map: dict[str, int] = dict.fromkeys(canonical_keys, ZERO_INT)
    secs_map: dict[str, float] = dict.fromkeys(canonical_keys, ZERO_FLOAT)
    sources_map: dict[str, set[str]] = {key: set() for key in canonical_keys}

    for entry in daily_entries:
        wp = entry.workplace
        days_map[wp] = days_map.get(wp, ZERO_INT) + SINGLE_DAY_DELTA
        secs_map[wp] = secs_map.get(wp, ZERO_FLOAT) + entry.tracked_seconds
        if entry.source != PROVENANCE_NONE:
            sources_map.setdefault(wp, set()).add(entry.source)

    hours_map = {
        wp: round(secs / SECONDS_PER_HOUR, HOURS_PRECISION) for wp, secs in secs_map.items()
    }
    summary_rows = tuple(
        WorkplaceSummaryRow(
            workplace=wp,
            day_count=days_map[wp],
            tracked_seconds=secs_map[wp],
            tracked_hours=hours_map[wp],
            sources=tuple(sorted(sources_map.get(wp, set()))),
        )
        for wp in days_map
        if wp in BUILTIN_WORKPLACE_VALUES or days_map[wp] > ZERO_INT
    )
    unclassified_days = days_map.get(WORKPLACE_UNCLASSIFIED, ZERO_INT)
    total_days = sum(days_map.values())
    total_secs = sum(secs_map.values())
    return WorkplaceTaxReport(
        start_date=start_iso,
        end_date=end_iso,
        summary_rows=summary_rows,
        daily_entries=daily_entries,
        days_by_workplace=days_map,
        hours_by_workplace=hours_map,
        seconds_by_workplace=secs_map,
        total_classified_days=total_days - unclassified_days,
        total_unclassified_days=unclassified_days,
        total_tracked_seconds=total_secs,
        total_tracked_hours=round(total_secs / SECONDS_PER_HOUR, HOURS_PRECISION),
    )


def _validate_datetime_window(
    since: datetime | None,
    until: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """Validate optional UTC `[since, until)` bounds."""
    try:
        utc_since = None if since is None else ensure_utc(since, field_name="since")
        utc_until = None if until is None else ensure_utc(until, field_name="until")
    except Exception as exc:
        raise ExporterValidationError(str(exc)) from exc
    if utc_since is not None and utc_until is not None and utc_until <= utc_since:
        raise ExporterValidationError("until must be strictly greater than since.")
    return utc_since, utc_until


def _sum_clipped_intervals(
    ledger: Ledger,
    since: datetime | None,
    until: datetime | None,
) -> float:
    """Sum closed `Interval` durations clipped to `[since, until)`."""
    intervals = ledger.list_intervals(since=since, until=until, include_open=False)
    total = ZERO_FLOAT
    for interval in intervals:
        if interval.ended_at is None:
            continue
        eff_start = interval.started_at if since is None else max(interval.started_at, since)
        eff_end = interval.ended_at if until is None else min(interval.ended_at, until)
        if eff_end > eff_start:
            total += (eff_end - eff_start).total_seconds()
    return total


# endregion
