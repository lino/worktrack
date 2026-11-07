"""Behavioural tests for iCal, JMAP, and MS 365 calendar `Sources` and bucket mapping."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from worktrack.connectors.calendar import (
    CALENDAR_SOURCE_SLUG,
    EVENT_STATUS_CANCELLED,
    EVENT_STATUS_CONFIRMED,
    PROVIDER_ICAL,
    PROVIDER_JMAP,
    PROVIDER_MS365,
    CalendarAuthenticationError,
    CalendarBucketMapping,
    CalendarBucketRule,
    CalendarConfigurationError,
    CalendarEvent,
    CalendarParseError,
    CalendarTransportError,
    IcalCalendarSource,
    JmapCalendarSource,
    Ms365CalendarSource,
    parse_ical_events,
)
from worktrack.core import ACTIVITY_KIND_STANDING, ACTIVITY_KIND_TARGETED, Ledger
from worktrack.outbox import OutboxStore, RollupEngine, RollupPolicy
from worktrack.secrets import OAuthTokenBundle, SecretValue, UserTokenStore
from worktrack.telemetry import TelemetryRecorder

# region Constants
SAMPLE_ICAL_FEED: Final = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Worktrack//Calendar Test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:ical-evt-standup-001\r\n"
    "SUMMARY:Daily Engineering\r\n  Standup\r\n"
    "DTSTART:20261107T090000Z\r\n"
    "DTEND:20261107T091500Z\r\n"
    "STATUS:CONFIRMED\r\n"
    "CATEGORIES:STANDUP,ENGINEERING\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:ical-evt-ticket-002\r\n"
    "SUMMARY:PROJ-123 Design Review\\, Architecture\r\n"
    "DTSTART;TZID=UTC:20261107T100000\r\n"
    "DURATION:PT45M\r\n"
    "STATUS:CONFIRMED\r\n"
    "CATEGORIES:ARCHITECTURE\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:ical-evt-cancelled-003\r\n"
    "SUMMARY:Cancelled Sync\r\n"
    "DTSTART:20261107T110000Z\r\n"
    "DTEND:20261107T113000Z\r\n"
    "STATUS:CANCELLED\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:ical-evt-allday-004\r\n"
    "SUMMARY:Company Holiday\r\n"
    "DTSTART;VALUE=DATE:20261107\r\n"
    "DTEND;VALUE=DATE:20261108\r\n"
    "STATUS:CONFIRMED\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

EXPECTED_ACTIVE_ICAL_EVENTS: Final = 2
EXPECTED_TOTAL_PARSED_ICAL_EVENTS: Final = 4
STANDUP_DURATION_SECONDS: Final = 900.0
DESIGN_REVIEW_DURATION_SECONDS: Final = 2700.0
ALL_HANDS_DURATION_SECONDS: Final = 3600.0
TRIMMED_OVERLAP_DURATION_SECONDS: Final = 1800.0
TOKEN_EXPIRY_SECONDS: Final = 3600
# endregion


# region Fixtures
class FakeJmapTransport:
    """In-memory JMAP `CalendarEvent/get` transport double."""

    def __init__(self, response: Mapping[str, object] | Exception) -> None:
        """Initialize transport with canned JMAP response or exception."""
        self.response = response
        self.calls: list[dict[str, object]] = []

    def fetch_events(
        self,
        *,
        account_id: str,
        calendar_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Mapping[str, object]:
        """Record call parameters and return canned JMAP response."""
        self.calls.append(
            {"account_id": account_id, "calendar_id": calendar_id, "since": since, "until": until}
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeMs365Transport:
    """In-memory Microsoft 365 Graph `/me/calendarView` transport double."""

    def __init__(self, response: Mapping[str, object] | Exception) -> None:
        """Initialize transport with canned MS 365 response or exception."""
        self.response = response
        self.calls: list[dict[str, object]] = []

    def fetch_calendar_view(
        self,
        *,
        bearer_token: str,
        calendar_id: str = "primary",
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Mapping[str, object]:
        """Record bearer token and query parameters and return Graph response."""
        self.calls.append(
            {
                "bearer_token": bearer_token,
                "calendar_id": calendar_id,
                "since": since,
                "until": until,
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _build_default_bucket_mapping() -> CalendarBucketMapping:
    """Construct a `CalendarBucketMapping` covering standups, 1:1s, and all-hands."""
    return CalendarBucketMapping(
        rules=(
            CalendarBucketRule(
                activity_name="Standups",
                external_ref="ENG-10",
                title_pattern=r"standup|daily\s+scrum",
                categories=("standup",),
                tags=("ceremony",),
            ),
            CalendarBucketRule(
                activity_name="1:1 Meetings",
                external_ref="MGT-20",
                title_pattern=r"\b1:1\b|one-on-one",
                categories=("1on1",),
            ),
            CalendarBucketRule(
                activity_name="All-Hands",
                external_ref="CORP-99",
                title_pattern=r"all[- ]hands|town\s+hall",
                categories=("all-hands",),
            ),
        ),
        default_standing_activity_name="General Meetings",
        default_standing_external_ref="MEET-1",
    )


def _jmap_item(uid: str, title: str, start: str, duration: str, keyword: str) -> dict[str, object]:
    """Construct a single RFC 8984 JMAP `CalendarEvent` dictionary."""
    return {
        "@type": "CalendarEvent",
        "id": f"id-{uid}",
        "uid": uid,
        "title": title,
        "start": start,
        "duration": duration,
        "status": "confirmed",
        "keywords": {keyword: True},
    }


def _sample_jmap_payload() -> dict[str, object]:
    """Build canned RFC 8984 JMAP `CalendarEvent/get` payload with two overlapping events."""
    items = [
        _jmap_item("uid-1on1", "Weekly 1:1 with Alex", "2026-11-07T13:00:00Z", "PT30M", "1on1"),
        _jmap_item(
            "uid-overlap", "Engineering All-Hands", "2026-11-07T13:15:00Z", "PT45M", "all-hands"
        ),
    ]
    return {"methodResponses": [["CalendarEvent/get", {"accountId": "acc-1", "list": items}, "0"]]}


def _sample_ms365_payload() -> dict[str, object]:
    """Build canned Microsoft 365 Graph `/me/calendarView` payload."""
    return {
        "value": [
            {
                "id": "ms-evt-1",
                "iCalUId": "ms-ical-uid-1",
                "subject": "Quarterly All-Hands",
                "start": {"dateTime": "2026-11-07T16:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-11-07T17:00:00.0000000", "timeZone": "UTC"},
                "isAllDay": False,
                "isCancelled": False,
                "showAs": "busy",
                "categories": ["All-Hands"],
            },
            {
                "id": "ms-evt-2",
                "iCalUId": "ms-ical-uid-2",
                "subject": "Vendor Sync",
                "start": {"dateTime": "2026-11-07T17:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-11-07T17:30:00.0000000", "timeZone": "UTC"},
                "isAllDay": False,
                "isCancelled": False,
                "showAs": "busy",
                "categories": [],
            },
        ]
    }


# endregion


# region Calendar Parser & Bucket Tests
def test_parse_ical_events_unfolds_lines_and_parses_vevent_fields() -> None:
    """Verify RFC 5545 line unfolding, `DURATION`, `CATEGORIES`, `STATUS`, and `VALUE=DATE`."""
    events = parse_ical_events(SAMPLE_ICAL_FEED, calendar_id="work")
    assert len(events) == EXPECTED_TOTAL_PARSED_ICAL_EVENTS

    standup = events[0]
    assert standup.uid == "ical-evt-standup-001"
    assert standup.title == "Daily Engineering Standup"
    assert standup.started_at == datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    assert standup.ended_at == datetime(2026, 11, 7, 9, 15, tzinfo=UTC)
    assert standup.status == EVENT_STATUS_CONFIRMED
    assert standup.categories == ("standup", "engineering")
    assert (standup.provider, standup.calendar_id, standup.is_all_day) == (
        PROVIDER_ICAL,
        "work",
        False,
    )
    assert standup.to_dict()["uid"] == "ical-evt-standup-001"

    review = events[1]
    assert (review.uid, review.title) == (
        "ical-evt-ticket-002",
        "PROJ-123 Design Review, Architecture",
    )
    assert review.ended_at == datetime(2026, 11, 7, 10, 45, tzinfo=UTC)
    assert events[2].status == EVENT_STATUS_CANCELLED
    assert events[3].is_all_day


def test_parse_ical_events_rejects_malformed_feed_and_invalid_rules() -> None:
    """Verify `parse_ical_events`, `CalendarEvent`, and `CalendarBucketRule` reject bad input."""
    with pytest.raises(CalendarParseError):
        parse_ical_events("NOT_AN_ICAL_FEED")
    with pytest.raises(CalendarParseError):
        parse_ical_events("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:No UID\r\nEND:VEVENT\r\n")
    with pytest.raises(CalendarConfigurationError):
        CalendarBucketRule(activity_name="Invalid Rule")
    with pytest.raises(CalendarConfigurationError):
        CalendarBucketRule(activity_name="Bad Regex", title_pattern="[unclosed")
    with pytest.raises(CalendarConfigurationError):
        CalendarEvent(
            uid="evt-1",
            title="Bad Event",
            started_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
            ended_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
        )


# endregion


# region Calendar Source Sync Tests
def test_ical_source_idempotent_sync_and_rollup_engine_integration(tmp_path: Path) -> None:
    """Verify `IcalCalendarSource` imports meetings idempotently and maps to `RollupEngine`."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    ics_file = tmp_path / "meetings.ics"
    ics_file.write_text(SAMPLE_ICAL_FEED, encoding="utf-8")
    source = IcalCalendarSource(
        ics_path=ics_file, bucket_mapping=_build_default_bucket_mapping(), telemetry=telemetry
    )

    with Ledger(":memory:", telemetry=telemetry) as ledger, OutboxStore(":memory:") as outbox:
        first = source.sync(ledger)
        assert first.provider == PROVIDER_ICAL
        assert first.events_fetched == EXPECTED_TOTAL_PARSED_ICAL_EVENTS
        assert first.activities_created == EXPECTED_ACTIVE_ICAL_EVENTS
        assert first.intervals_created == EXPECTED_ACTIVE_ICAL_EVENTS
        assert first.intervals_updated == 0
        assert first.intervals_skipped == EXPECTED_ACTIVE_ICAL_EVENTS
        assert first.to_dict()["provider"] == PROVIDER_ICAL

        intervals = ledger.list_intervals(include_open=False)
        assert [iv.duration_seconds for iv in intervals] == [
            STANDUP_DURATION_SECONDS,
            DESIGN_REVIEW_DURATION_SECONDS,
        ]
        standup_act = ledger.get_activity(intervals[0].activity_id or "")
        assert (standup_act.kind, standup_act.name) == (ACTIVITY_KIND_STANDING, "Standups")
        assert (standup_act.external_ref, standup_act.rollup_target_ref) == ("ENG-10", "ENG-10")
        targeted_act = ledger.get_activity(intervals[1].activity_id or "")
        assert (targeted_act.kind, targeted_act.external_ref) == (
            ACTIVITY_KIND_TARGETED,
            "PROJ-123",
        )

        second = source.sync(ledger)
        assert (second.intervals_created, second.intervals_updated) == (0, 0)

        engine = RollupEngine(ledger=ledger, outbox_store=outbox, telemetry=telemetry)
        preview = engine.preview_rollup(sink="jira", policy=RollupPolicy())
        target_refs = {prop.target_ref: prop.rounded_duration_seconds for prop in preview.proposals}
        assert target_refs == {"ENG-10": 900, "PROJ-123": 2700}

    metrics = telemetry.render_metrics_text()
    assert (
        f'worktrack_source_syncs_total{{outcome="ok",source="{CALENDAR_SOURCE_SLUG}"}} 2.0'
        in metrics
    )


def _headless_module_loader(module_name: str) -> object:
    """Raise `ImportError` so `UserTokenStore` uses its `0o600` file fallback."""
    raise ImportError(module_name)


def test_jmap_calendar_source_syncs_events_and_handles_reschedule_and_overlap(
    tmp_path: Path,
) -> None:
    """Verify `JmapCalendarSource` parses RFC 8984 responses, trims overlaps, and updates times."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    jmap_payload = _sample_jmap_payload()
    source = JmapCalendarSource(
        transport=FakeJmapTransport(jmap_payload),
        account_id="acc-1",
        bucket_mapping=_build_default_bucket_mapping(),
        telemetry=telemetry,
    )

    with Ledger(":memory:") as ledger:
        res1 = source.sync(ledger)
        assert (res1.provider, res1.intervals_created) == (
            PROVIDER_JMAP,
            EXPECTED_ACTIVE_ICAL_EVENTS,
        )
        intervals = ledger.list_intervals(include_open=False)
        assert len(intervals) == EXPECTED_ACTIVE_ICAL_EVENTS
        assert intervals[0].ended_at == datetime(2026, 11, 7, 13, 30, tzinfo=UTC)
        assert intervals[1].started_at == datetime(2026, 11, 7, 13, 30, tzinfo=UTC)
        assert intervals[1].duration_seconds == TRIMMED_OVERLAP_DURATION_SECONDS

        res2 = source.sync(ledger)
        assert (res2.intervals_created, res2.intervals_updated) == (0, 0)

        updated = [
            _jmap_item("uid-1on1", "Weekly 1:1 Extended", "2026-11-07T15:00:00Z", "PT45M", "1on1")
        ]
        jmap_payload["methodResponses"] = [
            ["CalendarEvent/get", {"accountId": "acc-1", "list": updated}, "0"]
        ]
        res3 = source.sync(ledger)
        assert (res3.intervals_created, res3.intervals_updated) == (0, 1)


def test_ms365_calendar_source_uses_user_token_store_and_records_errors(tmp_path: Path) -> None:
    """Verify `Ms365CalendarSource` authenticates via `UserTokenStore` and records error metrics."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    token_store = UserTokenStore(
        fallback_path=tmp_path / "tokens.json", module_loader=_headless_module_loader
    )
    transport = FakeMs365Transport(_sample_ms365_payload())
    source = Ms365CalendarSource(
        transport=transport,
        token_store=token_store,
        bucket_mapping=_build_default_bucket_mapping(),
        telemetry=telemetry,
    )

    with Ledger(":memory:") as ledger:
        with pytest.raises(CalendarAuthenticationError):
            source.sync(ledger)

        bundle = OAuthTokenBundle(
            access_token=SecretValue("ms365-secret-token-xyz"), expires_in=TOKEN_EXPIRY_SECONDS
        )
        token_store.store(PROVIDER_MS365, bundle)
        result = source.sync(ledger)
        assert result.provider == PROVIDER_MS365
        assert result.intervals_created == EXPECTED_ACTIVE_ICAL_EVENTS
        assert transport.calls[0]["bearer_token"] == "ms365-secret-token-xyz"

        intervals = ledger.list_intervals(include_open=False)
        assert intervals[0].duration_seconds == ALL_HANDS_DURATION_SECONDS
        fallback_act = ledger.get_activity(intervals[1].activity_id or "")
        assert (fallback_act.name, fallback_act.external_ref) == ("General Meetings", "MEET-1")

        bad_tx = FakeMs365Transport(CalendarTransportError("Graph 503 Unavailable"))
        failing = Ms365CalendarSource(
            transport=bad_tx, token_store=token_store, telemetry=telemetry
        )
        with pytest.raises(CalendarTransportError):
            failing.sync(ledger)

    metrics = telemetry.render_metrics_text()
    assert (
        f'worktrack_source_syncs_total{{outcome="ok",source="{CALENDAR_SOURCE_SLUG}"}} 1.0'
        in metrics
    )
    assert (
        f'worktrack_source_syncs_total{{outcome="error",source="{CALENDAR_SOURCE_SLUG}"}} 2.0'
        in metrics
    )


# endregion
