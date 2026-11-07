"""RFC 5545 iCalendar (`.ics`), JMAP (RFC 8984), and MS 365 Graph event parsers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from worktrack.connectors.calendar.exceptions import CalendarParseError
from worktrack.connectors.calendar.models import (
    ALLOWED_EVENT_STATUSES,
    DEFAULT_CALENDAR_TARGET_REF,
    DEFAULT_UNTITLED_MEETING_SUMMARY,
    EVENT_STATUS_CANCELLED,
    EVENT_STATUS_CONFIRMED,
    EVENT_STATUS_TENTATIVE,
    PROVIDER_ICAL,
    PROVIDER_JMAP,
    PROVIDER_MS365,
    CalendarEvent,
)

# region Constants
VCALENDAR_BEGIN: Final = "BEGIN:VCALENDAR"
VEVENT_BEGIN: Final = "BEGIN:VEVENT"
VEVENT_END: Final = "END:VEVENT"

ICAL_DATE_ONLY_LEN: Final = 8
ICAL_DATETIME_LOCAL_LEN: Final = 15
ICAL_DATETIME_UTC_LEN: Final = 16
DEFAULT_ALL_DAY_DAYS: Final = 1

FREE_BUSY_FREE: Final = "free"
MS365_RESPONSE_DECLINED: Final = "declined"
MS365_SHOW_AS_TENTATIVE: Final = "tentative"
SEVEN_DIGIT_FRACTION_PATTERN: Final = re.compile(r"(\.\d{6})\d+")

ISO_DURATION_PATTERN: Final = re.compile(
    r"^P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
    re.IGNORECASE,
)
UTC_TZ_ALIASES: Final[frozenset[str]] = frozenset({"utc", "gmt", "etc/utc", "etc/gmt", "z"})
# endregion


# region iCal Parser
def parse_ical_events(
    ics_text: str,
    *,
    calendar_id: str = DEFAULT_CALENDAR_TARGET_REF,
) -> list[CalendarEvent]:
    """Parse RFC 5545 `VEVENT` blocks from `ics_text` into normalized `CalendarEvents`."""
    unfolded = unfold_ical_lines(ics_text)
    if not any(line.upper() == VCALENDAR_BEGIN for line in unfolded):
        raise CalendarParseError("iCalendar payload is missing 'BEGIN:VCALENDAR'.")
    blocks = _extract_vevent_blocks(unfolded)
    return [_parse_vevent_block(block, calendar_id=calendar_id) for block in blocks]


def unfold_ical_lines(ics_text: str) -> list[str]:
    """Unfold RFC 5545 continuation lines (leading space or horizontal tab)."""
    normalized = ics_text.replace("\r\n", "\n").replace("\r", "\n")
    unfolded: list[str] = []
    for raw_line in normalized.split("\n"):
        if raw_line.startswith((" ", "\t")):
            if unfolded:
                unfolded[-1] += raw_line[1:]
            continue
        stripped = raw_line.strip()
        if stripped:
            unfolded.append(stripped)
    return unfolded


def _extract_vevent_blocks(lines: Sequence[str]) -> list[list[str]]:
    """Collect property lines between `BEGIN:VEVENT` and `END:VEVENT` markers."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        upper = line.upper()
        if upper == VEVENT_BEGIN:
            if current is not None:
                raise CalendarParseError("Nested 'BEGIN:VEVENT' encountered in iCalendar feed.")
            current = []
        elif upper == VEVENT_END:
            if current is None:
                raise CalendarParseError("Unmatched 'END:VEVENT' encountered in iCalendar feed.")
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    if current is not None:
        raise CalendarParseError("Unclosed 'BEGIN:VEVENT' encountered in iCalendar feed.")
    return blocks


def _parse_vevent_block(lines: Sequence[str], *, calendar_id: str) -> CalendarEvent:
    """Convert one unfolded `VEVENT` property list into a `CalendarEvent`."""
    props: dict[str, tuple[dict[str, str], str]] = {}
    categories: list[str] = []
    for line in lines:
        name, params, value = _split_property_line(line)
        if name == "CATEGORIES":
            categories.extend(item.strip() for item in value.split(",") if item.strip())
        else:
            props[name] = (params, value)

    uid = _resolve_vevent_uid(props)
    summary = _unescape_ical_text(props.get("SUMMARY", ({}, ""))[1]).strip()
    title = summary or DEFAULT_UNTITLED_MEETING_SUMMARY
    started_at, ended_at, is_all_day = _resolve_vevent_window(props, uid=uid)
    raw_status = props.get("STATUS", ({}, EVENT_STATUS_CONFIRMED))[1].strip().lower()
    status = raw_status if raw_status in ALLOWED_EVENT_STATUSES else EVENT_STATUS_CONFIRMED
    return CalendarEvent(
        uid=uid,
        title=title,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        categories=tuple(categories),
        provider=PROVIDER_ICAL,
        calendar_id=calendar_id,
        is_all_day=is_all_day,
    )


def _split_property_line(line: str) -> tuple[str, dict[str, str], str]:
    """Split `NAME;PARAM=VAL:VALUE` into `(UPPER_NAME, params_dict, raw_value)`."""
    if ":" not in line:
        raise CalendarParseError(f"Malformed iCalendar property line '{line}'.")
    head, value = line.split(":", 1)
    parts = head.split(";")
    prop_name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for raw_param in parts[1:]:
        if "=" in raw_param:
            k, v = raw_param.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return prop_name, params, value


def _resolve_vevent_uid(props: Mapping[str, tuple[dict[str, str], str]]) -> str:
    """Extract required `UID` and optional `RECURRENCE-ID` from a `VEVENT` block."""
    raw_uid = props.get("UID", ({}, ""))[1].strip()
    if not raw_uid:
        raise CalendarParseError("iCalendar VEVENT is missing required 'UID' property.")
    rec_id = props.get("RECURRENCE-ID", ({}, ""))[1].strip()
    return f"{raw_uid}#{rec_id}" if rec_id else raw_uid


def _resolve_vevent_window(
    props: Mapping[str, tuple[dict[str, str], str]],
    *,
    uid: str,
) -> tuple[datetime, datetime, bool]:
    """Resolve `(started_at, ended_at, is_all_day)` from `DTSTART`, `DTEND`, or `DURATION`."""
    if "DTSTART" not in props:
        raise CalendarParseError(f"iCalendar VEVENT '{uid}' is missing 'DTSTART'.")
    start_params, start_val = props["DTSTART"]
    started_at, is_all_day = parse_ical_datetime(start_val, start_params)

    if "DTEND" in props:
        end_params, end_val = props["DTEND"]
        ended_at, _ = parse_ical_datetime(end_val, end_params)
    elif "DURATION" in props:
        ended_at = started_at + parse_iso_duration(props["DURATION"][1])
    elif is_all_day:
        ended_at = started_at + timedelta(days=DEFAULT_ALL_DAY_DAYS)
    else:
        raise CalendarParseError(f"iCalendar VEVENT '{uid}' requires 'DTEND' or 'DURATION'.")
    return started_at, ended_at, is_all_day


def parse_ical_datetime(
    raw_value: str,
    params: Mapping[str, str] | None = None,
) -> tuple[datetime, bool]:
    """Parse an RFC 5545 `DATE-TIME` or `DATE` value into `(utc_datetime, is_all_day)`."""
    cleaned = raw_value.strip()
    param_map = {} if params is None else params
    val_type = param_map.get("VALUE", "").upper()
    if val_type == "DATE" or (len(cleaned) == ICAL_DATE_ONLY_LEN and cleaned.isdigit()):
        try:
            dt = datetime.strptime(cleaned, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError as exc:
            raise CalendarParseError(f"Invalid iCalendar DATE '{cleaned}'.") from exc
        return dt, True

    tz = resolve_calendar_tzinfo(param_map.get("TZID"))
    if cleaned.endswith("Z") and len(cleaned) == ICAL_DATETIME_UTC_LEN:
        return _strptime_ical(cleaned[:-1], UTC), False
    if len(cleaned) == ICAL_DATETIME_LOCAL_LEN and "T" in cleaned:
        return _strptime_ical(cleaned, tz).astimezone(UTC), False
    return _parse_iso_with_tz(cleaned, tz), False


def _strptime_ical(raw_local: str, tz: tzinfo) -> datetime:
    """Parse `YYYYMMDDTHHMMSS` with `tz` or raise `CalendarParseError`."""
    try:
        return datetime.strptime(raw_local, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
    except ValueError as exc:
        raise CalendarParseError(f"Invalid iCalendar timestamp '{raw_local}'.") from exc


def resolve_calendar_tzinfo(tz_name: str | None) -> tzinfo:
    """Resolve an IANA or UTC timezone identifier, defaulting to `UTC`."""
    if tz_name is None or not tz_name.strip():
        return UTC
    cleaned = tz_name.strip()
    if cleaned.lower() in UTC_TZ_ALIASES:
        return UTC
    try:
        return ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as exc:
        raise CalendarParseError(f"Unknown calendar timezone '{cleaned}'.") from exc


def parse_iso_duration(raw_duration: str) -> timedelta:
    """Parse an ISO-8601 / RFC 5545 duration string (e.g. `PT45M`, `PT1H30M`, `P1D`)."""
    cleaned = raw_duration.strip()
    match = ISO_DURATION_PATTERN.match(cleaned)
    if match is None:
        raise CalendarParseError(f"Invalid ISO-8601 duration '{raw_duration}'.")
    parts = {k: int(v) if v is not None else 0 for k, v in match.groupdict().items()}
    delta = timedelta(
        weeks=parts["weeks"],
        days=parts["days"],
        hours=parts["hours"],
        minutes=parts["minutes"],
        seconds=parts["seconds"],
    )
    if delta <= timedelta(0):
        raise CalendarParseError(f"Duration '{raw_duration}' must be > 0 seconds.")
    return delta


def _unescape_ical_text(raw_text: str) -> str:
    r"""Unescape RFC 5545 text escapes (`\n`, `\,`, `\;`, `\\`)."""
    return (
        raw_text.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def format_ical_busy_event(
    *, uid: str, started_at: datetime, ended_at: datetime, summary: str
) -> str:
    """Render an opaque RFC 5545 `VCALENDAR` containing a private busy `VEVENT`."""
    utc_start = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    utc_end = ended_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_summary = (
        summary.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", " ")
    )
    return "\r\n".join(
        (
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Worktrack//FocusBlock Busy Sink//EN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{utc_start}",
            f"DTSTART:{utc_start}",
            f"DTEND:{utc_end}",
            f"SUMMARY:{safe_summary}",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "CLASS:PRIVATE",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        )
    )


def parse_jmap_calendar_events(
    raw_response: Mapping[str, object] | Sequence[Mapping[str, object]],
    *,
    calendar_id: str = DEFAULT_CALENDAR_TARGET_REF,
) -> list[CalendarEvent]:
    """Parse RFC 8984 JMAP `CalendarEvent/get` response into normalized `CalendarEvents`."""
    return [
        _parse_single_jmap_event(item, calendar_id=calendar_id)
        for item in _extract_jmap_event_list(raw_response)
        if str(item.get("freeBusyStatus", "busy")).lower() != FREE_BUSY_FREE
    ]


def _extract_jmap_event_list(
    raw_response: Mapping[str, object] | Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """Extract the `CalendarEvent` object list from a JMAP envelope or direct dict."""
    if not isinstance(raw_response, Mapping):
        return _filter_mapping_items(raw_response)
    if isinstance(raw_response.get("list"), list):
        return _filter_mapping_items(raw_response["list"])  # type: ignore[arg-type]
    method_responses = raw_response.get("methodResponses")
    if isinstance(method_responses, list):
        return _extract_from_method_responses(method_responses)
    raise CalendarParseError("JMAP response is missing 'list' or 'methodResponses'.")


def _extract_from_method_responses(
    method_responses: Sequence[object],
) -> list[Mapping[str, object]]:
    """Collect `list` entries from JMAP `methodResponses` invocation tuples."""
    collected: list[Mapping[str, object]] = []
    for call in method_responses:
        if isinstance(call, (list, tuple)) and len(call) >= 2 and isinstance(call[1], Mapping):
            call_list = call[1].get("list")
            if isinstance(call_list, list):
                collected.extend(_filter_mapping_items(call_list))
    return collected


def _filter_mapping_items(items: Sequence[object]) -> list[Mapping[str, object]]:
    """Filter a sequence to `Mapping[str, object]` entries."""
    return [entry for entry in items if isinstance(entry, Mapping)]


def _str_field(item: Mapping[str, object], *keys: str, default: str = "") -> str:
    """Return the first non-empty string value among `keys` in `item`, or `default`."""
    for key in keys:
        val = item.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return default


def _parse_single_jmap_event(item: Mapping[str, object], *, calendar_id: str) -> CalendarEvent:
    """Convert one RFC 8984 JMAP `CalendarEvent` mapping into a `CalendarEvent`."""
    uid = _str_field(item, "uid", "id")
    raw_start = _str_field(item, "start")
    if not uid or not raw_start:
        raise CalendarParseError("JMAP CalendarEvent requires non-empty 'uid'/'id' and 'start'.")
    title = _str_field(item, "title", "summary", default=DEFAULT_UNTITLED_MEETING_SUMMARY)
    tz = resolve_calendar_tzinfo(_str_field(item, "timeZone", default="UTC"))
    started_at = _parse_iso_with_tz(raw_start, tz)
    raw_end = _str_field(item, "end")
    ended_at = (
        _parse_iso_with_tz(raw_end, tz)
        if raw_end
        else started_at + parse_iso_duration(_str_field(item, "duration", default="PT30M"))
    )
    raw_status = _str_field(item, "status", default=EVENT_STATUS_CONFIRMED).lower()
    status = raw_status if raw_status in ALLOWED_EVENT_STATUSES else EVENT_STATUS_CONFIRMED
    raw_kw = item.get("keywords", item.get("categories"))
    return CalendarEvent(
        uid=uid,
        title=title,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        categories=_extract_jmap_keywords(raw_kw),
        provider=PROVIDER_JMAP,
        calendar_id=calendar_id,
        is_all_day=bool(item.get("showWithoutTime", False)),
    )


def _extract_jmap_keywords(raw_keywords: object) -> tuple[str, ...]:
    """Extract active keyword strings from a JMAP `String[Boolean]` map or list."""
    if isinstance(raw_keywords, Mapping):
        return tuple(str(k).strip() for k, active in raw_keywords.items() if active)
    if isinstance(raw_keywords, list):
        return tuple(str(k).strip() for k in raw_keywords if isinstance(k, str) and k.strip())
    return ()


def parse_ms365_calendar_events(
    raw_payload: Mapping[str, object],
    *,
    calendar_id: str = DEFAULT_CALENDAR_TARGET_REF,
) -> list[CalendarEvent]:
    """Parse Microsoft 365 Graph `/me/calendarView` JSON into normalized `CalendarEvents`."""
    raw_values = raw_payload.get("value")
    if not isinstance(raw_values, list):
        raise CalendarParseError("MS 365 calendarView payload is missing 'value' array.")
    return [
        _parse_single_ms365_event(item, calendar_id=calendar_id)
        for item in raw_values
        if isinstance(item, Mapping)
        and str(item.get("showAs", "busy")).strip().lower() != FREE_BUSY_FREE
    ]


def _parse_single_ms365_event(item: Mapping[str, object], *, calendar_id: str) -> CalendarEvent:
    """Convert one Microsoft Graph event dictionary into a normalized `CalendarEvent`."""
    uid = _str_field(item, "iCalUId", "id")
    if not uid:
        raise CalendarParseError("MS 365 event is missing 'iCalUId' and 'id'.")
    title = _str_field(item, "subject", default=DEFAULT_UNTITLED_MEETING_SUMMARY)
    started_at = _parse_ms365_datetime_slot(item.get("start"), uid=uid, field_name="start")
    ended_at = _parse_ms365_datetime_slot(item.get("end"), uid=uid, field_name="end")
    return CalendarEvent(
        uid=uid,
        title=title,
        started_at=started_at,
        ended_at=ended_at,
        status=_resolve_ms365_status(item),
        categories=_extract_jmap_keywords(item.get("categories")),
        provider=PROVIDER_MS365,
        calendar_id=calendar_id,
        is_all_day=bool(item.get("isAllDay", False)),
    )


def _parse_ms365_datetime_slot(slot: object, *, uid: str, field_name: str) -> datetime:
    """Parse Microsoft Graph `{'dateTime': ..., 'timeZone': ...}` into UTC `datetime`."""
    if not isinstance(slot, Mapping) or not isinstance(slot.get("dateTime"), str):
        raise CalendarParseError(f"MS 365 event '{uid}' has invalid '{field_name}' object.")
    raw_dt = SEVEN_DIGIT_FRACTION_PATTERN.sub(r"\1", str(slot["dateTime"]).strip())
    tz_str = str(slot.get("timeZone") or "UTC")
    return _parse_iso_with_tz(raw_dt, resolve_calendar_tzinfo(tz_str))


def _resolve_ms365_status(item: Mapping[str, object]) -> str:
    """Determine normalized event status (`confirmed`, `tentative`, `cancelled`) for MS 365."""
    if bool(item.get("isCancelled", False)):
        return EVENT_STATUS_CANCELLED
    resp_status = item.get("responseStatus")
    if isinstance(resp_status, Mapping):
        resp_val = str(resp_status.get("response") or "").strip().lower()
        if resp_val == MS365_RESPONSE_DECLINED:
            return EVENT_STATUS_CANCELLED
    if str(item.get("showAs") or "").strip().lower() == MS365_SHOW_AS_TENTATIVE:
        return EVENT_STATUS_TENTATIVE
    return EVENT_STATUS_CONFIRMED


def _parse_iso_with_tz(raw_iso: str, default_tz: tzinfo) -> datetime:
    """Parse an ISO-8601 timestamp string and attach `default_tz` when naive."""
    cleaned = raw_iso.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise CalendarParseError(f"Invalid calendar ISO-8601 timestamp '{raw_iso}'.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(UTC)


# endregion
