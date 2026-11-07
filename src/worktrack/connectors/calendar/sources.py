"""Idempotent iCal, JMAP (RFC 8984), and Microsoft 365 Graph calendar meeting `Sources`."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from worktrack.connectors.calendar import ical_parser as _p
from worktrack.connectors.calendar import models as _m
from worktrack.connectors.calendar.exceptions import (
    CalendarAuthenticationError,
    CalendarConfigurationError,
    CalendarTransportError,
)
from worktrack.connectors.calendar.models import (
    CalendarBucketMapping,
    CalendarEvent,
    CalendarSyncResult,
)
from worktrack.core import (
    ACTIVITY_KIND_STANDING,
    ACTIVITY_KIND_TARGETED,
    Activity,
    Interval,
    Ledger,
)
from worktrack.core.intervals import replace_interval_tags
from worktrack.core.models import compute_duration_us, ensure_utc, format_utc_iso, normalize_tags
from worktrack.core.schema import transaction
from worktrack.secrets import DEFAULT_TOKEN_ACCOUNT, SecretError, UserTokenStore
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
METRIC_SOURCE_SYNCS_TOTAL: Final = "worktrack_source_syncs_total"
OUTCOME_OK: Final = "ok"
OUTCOME_ERROR: Final = "error"
# endregion


# region Protocols
class JmapCalendarTransport(Protocol):
    """Injectable transport protocol for RFC 8984 JMAP `CalendarEvent/get` queries."""

    def fetch_events(
        self,
        *,
        account_id: str,
        calendar_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Mapping[str, object] | Sequence[Mapping[str, object]]:
        """Fetch RFC 8984 JMAP `CalendarEvent/get` response or event object sequence."""
        ...


class Ms365CalendarTransport(Protocol):
    """Injectable transport protocol for Microsoft 365 Graph `/me/calendarView`."""

    def fetch_calendar_view(
        self,
        *,
        bearer_token: str,
        calendar_id: str = _m.DEFAULT_CALENDAR_TARGET_REF,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Mapping[str, object]:
        """Fetch Microsoft Graph `/me/calendarView` JSON response using `bearer_token`."""
        ...


# endregion


# region Models
@dataclass(slots=True)
class _SyncAccumulator:
    """Mutable state and counters accumulated during a single calendar `Source` sync run."""

    ledger: Ledger
    mapping: CalendarBucketMapping
    include_tentative: bool = True
    include_all_day: bool = False
    since: datetime | None = None
    until: datetime | None = None
    events_fetched: int = 0
    activities_created: int = 0
    activities_updated: int = 0
    intervals_created: int = 0
    intervals_updated: int = 0
    intervals_skipped: int = 0


# endregion


# region Calendar Mapping
def sync_calendar_events(
    ledger: Ledger,
    events: Sequence[CalendarEvent],
    *,
    provider: str,
    bucket_mapping: CalendarBucketMapping,
    include_tentative: bool = True,
    include_all_day: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
) -> CalendarSyncResult:
    """Upsert non-overlapping `Intervals` and mapped `Activities` for `events`."""
    acc = _SyncAccumulator(
        ledger=ledger,
        mapping=bucket_mapping,
        include_tentative=include_tentative,
        include_all_day=include_all_day,
        since=None if since is None else ensure_utc(since, field_name="since"),
        until=None if until is None else ensure_utc(until, field_name="until"),
        events_fetched=len(events),
    )
    for event in events:
        if not _should_import_event(acc, event):
            acc.intervals_skipped += 1
            continue
        activity = _resolve_or_upsert_event_activity(acc, event)
        if activity is None:
            acc.intervals_skipped += 1
            continue
        _upsert_event_interval(acc, event, activity)
    return CalendarSyncResult(
        provider=provider,
        events_fetched=acc.events_fetched,
        activities_created=acc.activities_created,
        activities_updated=acc.activities_updated,
        intervals_created=acc.intervals_created,
        intervals_updated=acc.intervals_updated,
        intervals_skipped=acc.intervals_skipped,
    )


def _should_import_event(acc: _SyncAccumulator, event: CalendarEvent) -> bool:
    """Return `True` when `event` is eligible, or delete cancelled intervals."""
    if event.status == _m.EVENT_STATUS_CANCELLED:
        for item in acc.ledger.list_intervals(tag=event.event_tag, include_open=False):
            conn = acc.ledger._require_conn()
            with transaction(conn):
                conn.execute("DELETE FROM intervals WHERE id = ?", (item.id,))
        return False
    if event.status == _m.EVENT_STATUS_TENTATIVE and not acc.include_tentative:
        return False
    if event.is_all_day and not acc.include_all_day:
        return False
    if acc.since is not None and event.ended_at <= acc.since:
        return False
    return not (acc.until is not None and event.started_at >= acc.until)


def _resolve_or_upsert_event_activity(
    acc: _SyncAccumulator, event: CalendarEvent
) -> Activity | None:
    """Resolve or upsert a *Targeted* or *Standing* `Activity` for `event`."""
    ticket_key = acc.mapping.extract_ticket_key(event.title)
    if ticket_key is not None:
        existing = acc.ledger.find_activity_by_external_ref(ticket_key)
        if existing is not None:
            return None if existing.archived else existing
        if not acc.mapping.create_missing_targeted_activities:
            return None
        acc.activities_created += 1
        return acc.ledger.create_activity(
            event.title,
            kind=ACTIVITY_KIND_TARGETED,
            external_ref=ticket_key,
            source=_m.CALENDAR_SOURCE_SLUG,
            tags=(_m.CALENDAR_SOURCE_SLUG,),
        )
    rule = acc.mapping.match_standing_rule(event)
    if rule is not None:
        spec = (rule.activity_name, rule.external_ref, rule.rollup_target_ref)
        return _upsert_standing(acc, spec, rule.tags)
    ref = acc.mapping.default_standing_external_ref
    return _upsert_standing(acc, (acc.mapping.default_standing_activity_name, ref, ref), ())


def _upsert_standing(
    acc: _SyncAccumulator, spec: tuple[str, str | None, str | None], tags: Sequence[str]
) -> Activity:
    """Find or create a *Standing* `Activity` and keep its rollup refs synchronized."""
    name, ext_ref, rollup_ref = spec
    merged_tags = normalize_tags((_m.CALENDAR_SOURCE_SLUG, *tags))
    lower_name = name.strip().lower()
    existing: Activity | None = None
    for act in acc.ledger.list_activities(kind=ACTIVITY_KIND_STANDING, include_archived=True):
        if (ext_ref is not None and act.external_ref == ext_ref) or (
            act.name.strip().lower() == lower_name
        ):
            existing = act
            break
    if existing is None:
        acc.activities_created += 1
        return acc.ledger.create_activity(
            name,
            kind=ACTIVITY_KIND_STANDING,
            external_ref=ext_ref,
            rollup_target_ref=rollup_ref,
            source=_m.CALENDAR_SOURCE_SLUG,
            tags=merged_tags,
        )
    if (
        existing.archived
        or (existing.name, existing.external_ref, existing.rollup_target_ref) != spec
    ):
        acc.activities_updated += 1
        return acc.ledger.update_activity(
            existing.id,
            name=name,
            external_ref=ext_ref,
            rollup_target_ref=rollup_ref,
            archived=False,
            tags=merged_tags,
        )
    return existing


def _upsert_event_interval(acc: _SyncAccumulator, event: CalendarEvent, activity: Activity) -> None:
    """Create or update a non-overlapping structured `Interval` for `event`."""
    matches = acc.ledger.list_intervals(tag=event.event_tag, include_open=False)
    existing = matches[0] if matches else None
    excl_id = None if existing is None else existing.id
    window = _resolve_non_overlapping_window(acc.ledger, event.started_at, event.ended_at, excl_id)
    if window is None:
        acc.intervals_skipped += 1
        return
    start_dt, end_dt = window
    tags = normalize_tags((_m.CALENDAR_SOURCE_SLUG, event.provider, event.event_tag))
    if existing is None:
        acc.ledger.record_structured_interval(
            activity.id, started_at=start_dt, ended_at=end_dt, notes=event.title, tags=tags
        )
        acc.intervals_created += 1
        return
    if (existing.started_at, existing.ended_at, existing.activity_id, existing.notes) == (
        start_dt,
        end_dt,
        activity.id,
        event.title,
    ):
        return
    conn = acc.ledger._require_conn()
    s_iso, e_iso = format_utc_iso(start_dt), format_utc_iso(end_dt)
    dur_us = compute_duration_us(start_dt, end_dt)
    now_iso = format_utc_iso(datetime.now(UTC))
    sql = (
        "UPDATE intervals SET started_at = ?, ended_at = ?, duration_us = ?, "
        "activity_id = ?, notes = ?, updated_at = ? WHERE id = ?"
    )
    with transaction(conn):
        conn.execute(sql, (s_iso, e_iso, dur_us, activity.id, event.title, now_iso, existing.id))
        replace_interval_tags(conn, existing.id, tags)
    acc.intervals_updated += 1


def _resolve_non_overlapping_window(
    ledger: Ledger, started_at: datetime, ended_at: datetime, exclude_id: str | None
) -> tuple[datetime, datetime] | None:
    """Trim `[started_at, ended_at)` against existing closed `Intervals` or return `None`."""
    cur_start, cur_end = started_at, ended_at
    for other in ledger.list_intervals(since=cur_start, until=cur_end, include_open=False):
        if other.id == exclude_id or other.ended_at is None:
            continue
        cur_start, cur_end = _clip_against_interval(cur_start, cur_end, other)
        if cur_end <= cur_start:
            return None
    return cur_start, cur_end


def _clip_against_interval(
    cur_start: datetime, cur_end: datetime, other: Interval
) -> tuple[datetime, datetime]:
    """Clip `[cur_start, cur_end)` against a single closed `other` `Interval`."""
    assert other.ended_at is not None
    ov_start, ov_end = other.started_at, other.ended_at
    if ov_end <= cur_start or ov_start >= cur_end:
        return cur_start, cur_end
    if ov_start <= cur_start and ov_end >= cur_end:
        return cur_end, cur_start
    if ov_start <= cur_start < ov_end < cur_end:
        return ov_end, cur_end
    if cur_start < ov_start < cur_end <= ov_end:
        return cur_start, ov_start
    return (
        (cur_start, ov_start) if (ov_start - cur_start) >= (cur_end - ov_end) else (ov_end, cur_end)
    )


# endregion


# region Calendar Sources
@dataclass(kw_only=True)
class _BaseCalendarSource:
    """Shared configuration and telemetry wrapper for calendar `Source` connectors."""

    bucket_mapping: CalendarBucketMapping | None = None
    calendar_id: str = _m.DEFAULT_CALENDAR_TARGET_REF
    include_tentative: bool = True
    include_all_day: bool = False
    telemetry: TelemetryRecorder | None = None
    _provider: str = _m.PROVIDER_ICAL

    def sync(
        self, ledger: Ledger, *, since: datetime | None = None, until: datetime | None = None
    ) -> CalendarSyncResult:
        """Fetch calendar events and upsert mapped `Activities` and `Intervals` into `ledger`."""
        return self._execute_sync(ledger, since, until, lambda: self._fetch(since, until))

    def _fetch(self, since: datetime | None, until: datetime | None) -> list[CalendarEvent]:
        """Fetch raw provider events and parse them into `CalendarEvent` instances."""
        raise NotImplementedError

    def _execute_sync(
        self,
        ledger: Ledger,
        since: datetime | None,
        until: datetime | None,
        loader: Callable[[], Sequence[CalendarEvent]],
    ) -> CalendarSyncResult:
        """Load events via `loader`, sync into `ledger`, and emit source telemetry."""
        mapping = CalendarBucketMapping() if self.bucket_mapping is None else self.bucket_mapping
        try:
            result = sync_calendar_events(
                ledger,
                loader(),
                provider=self._provider,
                bucket_mapping=mapping,
                include_tentative=self.include_tentative,
                include_all_day=self.include_all_day,
                since=since,
                until=until,
            )
        except Exception:
            if self.telemetry is not None:
                labels = {"source": _m.CALENDAR_SOURCE_SLUG, "outcome": OUTCOME_ERROR}
                self.telemetry.increment_counter(METRIC_SOURCE_SYNCS_TOTAL, labels)
            raise
        if self.telemetry is not None:
            labels = {"source": _m.CALENDAR_SOURCE_SLUG, "outcome": OUTCOME_OK}
            self.telemetry.increment_counter(METRIC_SOURCE_SYNCS_TOTAL, labels)
        return result


@dataclass(kw_only=True)
class IcalCalendarSource(_BaseCalendarSource):
    """RFC 5545 `.ics` calendar `Source` syncing meetings into `Ledger`."""

    ics_path: Path | str | None = None
    ics_url: str | None = None
    ics_content: str | None = None
    http_fetcher: Callable[[str], str] | None = None
    _provider: str = _m.PROVIDER_ICAL

    def sync(
        self,
        ledger: Ledger,
        *,
        ics_content: str | None = None,
        ics_path: Path | str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> CalendarSyncResult:
        """Parse RFC 5545 `.ics` events and upsert meeting `Activities` and `Intervals`."""
        cal_id = self.calendar_id.strip() or _m.DEFAULT_CALENDAR_TARGET_REF
        return self._execute_sync(
            ledger,
            since,
            until,
            lambda: _p.parse_ical_events(self._load_ics(ics_content, ics_path), calendar_id=cal_id),
        )

    def _load_ics(self, ics_content: str | None, ics_path: Path | str | None) -> str:
        """Load `.ics` text from explicit content, file path, or configured URL fetcher."""
        if ics_content is not None or self.ics_content is not None:
            return ics_content if ics_content is not None else str(self.ics_content)
        raw_path = self.ics_path if ics_path is None else ics_path
        if raw_path is not None:
            try:
                return Path(raw_path).read_text(encoding="utf-8")
            except OSError as exc:
                raise CalendarTransportError(f"Cannot read iCal file '{raw_path}': {exc}") from exc
        if self.ics_url is not None and self.http_fetcher is not None:
            return self.http_fetcher(self.ics_url)
        raise CalendarConfigurationError(
            "IcalCalendarSource requires ics_content, ics_path, or ics_url + http_fetcher."
        )


@dataclass(kw_only=True)
class JmapCalendarSource(_BaseCalendarSource):
    """RFC 8984 JMAP `CalendarEvent/get` meeting `Source`."""

    transport: JmapCalendarTransport
    account_id: str = _m.DEFAULT_JMAP_ACCOUNT_ID
    _provider: str = _m.PROVIDER_JMAP

    def _fetch(self, since: datetime | None, until: datetime | None) -> list[CalendarEvent]:
        """Query JMAP transport and parse RFC 8984 `CalendarEvent` list."""
        acc_id = self.account_id.strip() or _m.DEFAULT_JMAP_ACCOUNT_ID
        cal_id = self.calendar_id.strip() or _m.DEFAULT_CALENDAR_TARGET_REF
        raw = self.transport.fetch_events(
            account_id=acc_id, calendar_id=cal_id, since=since, until=until
        )
        return _p.parse_jmap_calendar_events(raw, calendar_id=cal_id)


@dataclass(kw_only=True)
class Ms365CalendarSource(_BaseCalendarSource):
    """Microsoft 365 Graph `/me/calendarView` meeting `Source` using `UserTokenStore`."""

    transport: Ms365CalendarTransport
    token_store: UserTokenStore
    provider_name: str = _m.PROVIDER_MS365
    account: str = DEFAULT_TOKEN_ACCOUNT
    _provider: str = _m.PROVIDER_MS365

    def _fetch(self, since: datetime | None, until: datetime | None) -> list[CalendarEvent]:
        """Load bearer token, query MS 365 transport, and parse Graph `/me/calendarView`."""
        cal_id = self.calendar_id.strip() or _m.DEFAULT_CALENDAR_TARGET_REF
        raw = self.transport.fetch_calendar_view(
            bearer_token=self._load_bearer_token(), calendar_id=cal_id, since=since, until=until
        )
        return _p.parse_ms365_calendar_events(raw, calendar_id=cal_id)

    def _load_bearer_token(self) -> str:
        """Load OAuth access token from `UserTokenStore` or raise `CalendarAuthenticationError`."""
        prov = self.provider_name.strip() or _m.PROVIDER_MS365
        try:
            bundle = self.token_store.load(prov, self.account)
        except SecretError as exc:
            raise CalendarAuthenticationError(f"MS 365 token load failed: {exc}") from exc
        if bundle is None:
            raise CalendarAuthenticationError(f"No MS 365 OAuth token found for '{prov}'.")
        return bundle.access_token.expose()


# endregion
