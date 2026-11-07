"""Deterministic rounding, SHA-256 hashing, payload hygiene, and cadence rules."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final

from worktrack.core.models import MICROSECONDS_PER_SECOND, ensure_utc, format_utc_iso
from worktrack.outbox.exceptions import OutboxValidationError
from worktrack.outbox.models import (
    ALLOWED_FLUSH_CADENCES,
    ALLOWED_FLUSH_TRIGGERS,
    ALLOWED_ROUNDING_MODES,
    DEFAULT_EOD_HOUR_UTC,
    DEFAULT_EOW_WEEKDAY,
    FLUSH_CADENCE_EOD,
    FLUSH_CADENCE_EOM,
    FLUSH_CADENCE_EOW,
    FLUSH_CADENCE_MANUAL,
    FLUSH_CADENCE_ON_CLOCK_OUT,
    FLUSH_TRIGGER_CLOCK_OUT,
    FLUSH_TRIGGER_MANUAL,
    FLUSH_TRIGGER_SCHEDULE,
    FORBIDDEN_PAYLOAD_KEYS,
    MIN_POSITIVE_QUANTUM_SECONDS,
    ROUNDING_MODE_CEIL,
    ROUNDING_MODE_FLOOR,
    ROUNDING_MODE_NEAREST,
    _validate_schedule_bounds,
)

# region Constants
HALF_DIVISOR: Final = 2
ONE_DAY_STEP: Final = 1
# endregion


# region Validation
def canonical_payload_json(payload: Mapping[str, object]) -> str:
    """Serialize `payload` to canonical compact UTF-8 JSON with sorted keys."""
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise OutboxValidationError(f"Outbox payload must be JSON-serializable: {exc}") from exc


def compute_payload_sha256(payload: Mapping[str, object]) -> str:
    """Compute canonical SHA-256 hex digest over `payload`."""
    encoded = canonical_payload_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_rollup_idempotency_key(
    *,
    sink: str,
    target_ref: str,
    window_start: datetime,
    window_end: datetime,
    source_interval_ids: Sequence[str],
) -> str:
    """Derive deterministic SHA-256 `idempotency_key` from sink, target, window, and intervals."""
    clean_ids = sorted({item.strip() for item in source_interval_ids if item.strip()})
    if not clean_ids:
        raise OutboxValidationError("source_interval_ids must contain at least one Interval ID.")
    material = {
        "sink": sink.strip().lower(),
        "target_ref": target_ref.strip(),
        "window_start": format_utc_iso(ensure_utc(window_start, field_name="window_start")),
        "window_end": format_utc_iso(ensure_utc(window_end, field_name="window_end")),
        "source_interval_ids": clean_ids,
    }
    return compute_payload_sha256(material)


def round_duration_seconds(
    duration_seconds: float,
    *,
    quantum_seconds: int,
    mode: str = ROUNDING_MODE_NEAREST,
) -> int:
    """Round `duration_seconds` to `quantum_seconds` using exact microsecond integer math."""
    if isinstance(quantum_seconds, bool) or quantum_seconds < MIN_POSITIVE_QUANTUM_SECONDS:
        raise OutboxValidationError("quantum_seconds must be an integer >= 1.")
    if mode not in ALLOWED_ROUNDING_MODES:
        raise OutboxValidationError(f"Unsupported rounding mode '{mode}'.")
    if duration_seconds <= 0:
        return 0
    total_us = round(duration_seconds * MICROSECONDS_PER_SECOND)
    quantum_us = quantum_seconds * MICROSECONDS_PER_SECOND
    if mode == ROUNDING_MODE_FLOOR:
        units = total_us // quantum_us
    elif mode == ROUNDING_MODE_CEIL:
        units = (total_us + quantum_us - 1) // quantum_us
    elif mode == ROUNDING_MODE_NEAREST:
        units = (total_us + (quantum_us // HALF_DIVISOR)) // quantum_us
    else:
        raise OutboxValidationError(f"Unsupported rounding mode '{mode}'.")
    return int(units * quantum_seconds)


def validate_payload_hygiene(payload: Mapping[str, object]) -> None:
    """Reject outbound payloads containing raw `Intervals`, `notes`, or `Clues` keys."""
    violating = sorted(key for key in payload if key.lower() in FORBIDDEN_PAYLOAD_KEYS)
    if violating:
        joined = ", ".join(violating)
        raise OutboxValidationError(
            f"Anti-surveillance violation: forbidden raw key(s) in outbound payload: {joined}."
        )
    for value in payload.values():
        if isinstance(value, Mapping):
            validate_payload_hygiene(value)


def should_flush_for_cadence(
    cadence: str,
    *,
    now: datetime,
    reference_time: datetime | None = None,
    trigger: str = FLUSH_TRIGGER_SCHEDULE,
    eod_hour_utc: int = DEFAULT_EOD_HOUR_UTC,
    eow_weekday: int = DEFAULT_EOW_WEEKDAY,
) -> bool:
    """Determine whether an `OutboxItem` with `cadence` is eligible to flush at `now`."""
    if cadence not in ALLOWED_FLUSH_CADENCES:
        raise OutboxValidationError(f"Unsupported flush cadence '{cadence}'.")
    if trigger not in ALLOWED_FLUSH_TRIGGERS:
        raise OutboxValidationError(f"Unsupported flush trigger '{trigger}'.")
    _validate_schedule_bounds(eod_hour_utc, eow_weekday)
    utc_now = ensure_utc(now, field_name="now")
    utc_ref = (
        utc_now
        if reference_time is None
        else ensure_utc(reference_time, field_name="reference_time")
    )

    if trigger == FLUSH_TRIGGER_MANUAL:
        return True
    if cadence == FLUSH_CADENCE_MANUAL:
        return False
    if cadence == FLUSH_CADENCE_ON_CLOCK_OUT:
        return trigger == FLUSH_TRIGGER_CLOCK_OUT
    if cadence == FLUSH_CADENCE_EOD:
        return _is_eod_due(utc_now, utc_ref, eod_hour_utc)
    if cadence == FLUSH_CADENCE_EOW:
        return _is_eow_due(utc_now, utc_ref, eod_hour_utc, eow_weekday)
    return _is_eom_due(utc_now, utc_ref, eod_hour_utc)


def _is_eod_due(utc_now: datetime, utc_ref: datetime, eod_hour_utc: int) -> bool:
    """Return `True` when `utc_now` has reached End-of-Day relative to `utc_ref`."""
    if utc_now.date() > utc_ref.date():
        return True
    return utc_now.date() == utc_ref.date() and utc_now.hour >= eod_hour_utc


def _is_eow_due(
    utc_now: datetime,
    utc_ref: datetime,
    eod_hour_utc: int,
    eow_weekday: int,
) -> bool:
    """Return `True` when `utc_now` has reached End-of-Week relative to `utc_ref`."""
    now_iso_week = utc_now.isocalendar()[:2]
    ref_iso_week = utc_ref.isocalendar()[:2]
    if now_iso_week > ref_iso_week:
        return True
    if now_iso_week < ref_iso_week:
        return False
    if utc_now.weekday() > eow_weekday:
        return True
    return utc_now.weekday() == eow_weekday and utc_now.hour >= eod_hour_utc


def _is_eom_due(utc_now: datetime, utc_ref: datetime, eod_hour_utc: int) -> bool:
    """Return `True` when `utc_now` has reached End-of-Month relative to `utc_ref`."""
    assert FLUSH_CADENCE_EOM == "eom"
    now_ym = (utc_now.year, utc_now.month)
    ref_ym = (utc_ref.year, utc_ref.month)
    if now_ym > ref_ym:
        return True
    if now_ym < ref_ym:
        return False
    is_last_day = (utc_now.date() + timedelta(days=ONE_DAY_STEP)).month != utc_now.month
    return is_last_day and utc_now.hour >= eod_hour_utc


# endregion
