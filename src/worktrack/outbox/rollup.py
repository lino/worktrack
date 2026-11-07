"""`collectd`-style `RollupEngine` aggregating closed `Intervals` into previewable rollups."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from worktrack.core import ACTIVITY_KIND_STANDING, Activity, Interval, Ledger
from worktrack.core.models import ISO_DATE_LENGTH, format_utc_iso
from worktrack.outbox.exceptions import OutboxValidationError
from worktrack.outbox.models import (
    CHANGE_KIND_CREATE,
    CHANGE_KIND_UNCHANGED,
    CHANGE_KIND_UPDATE,
    DEFAULT_REDACTED_SUMMARY_TEMPLATE,
    GROUPING_WINDOW_DAY,
    GROUPING_WINDOW_INTERVAL,
    GROUPING_WINDOW_MONTH,
    GROUPING_WINDOW_WEEK,
    OutboxItem,
    PreviewDiff,
    RollupPolicy,
    RollupPreviewResult,
    RollupProposal,
)
from worktrack.outbox.policies import round_duration_seconds
from worktrack.outbox.store import OutboxStore
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
OUTCOME_OK: Final = "ok"
OUTCOME_ERROR: Final = "error"
DAYS_PER_WEEK: Final = 7
DECEMBER_MONTH: Final = 12
JANUARY_MONTH: Final = 1
FIRST_DAY_OF_MONTH: Final = 1
NOTE_JOIN_SEPARATOR: Final = "; "
# endregion


# region Models
@dataclass(slots=True)
class _BucketAccumulator:
    """Mutable in-memory accumulator for a single `(target_ref, window)` rollup bucket."""

    target_ref: str
    activity_name: str
    window_start: datetime
    window_end: datetime
    window_start_iso: str
    window_end_iso: str
    raw_duration_seconds: float = 0.0
    interval_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# endregion


# region Rollup Engine
class RollupEngine:
    """Aggregate closed `Ledger` `Intervals` into redacted `RollupProposals` and `PreviewDiffs`."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        outbox_store: OutboxStore,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        """Initialize rollup engine over `ledger` and `outbox_store`."""
        self._ledger = ledger
        self._outbox_store = outbox_store
        self._telemetry = telemetry

    def preview_rollup(
        self,
        *,
        sink: str,
        policy: RollupPolicy | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> RollupPreviewResult:
        """Compute a non-mutating dry-run `RollupPreviewResult` and record telemetry."""
        active_policy = RollupPolicy() if policy is None else policy
        started_mono = time.perf_counter()
        try:
            clean_sink = sink.strip().lower()
            if not clean_sink:
                raise OutboxValidationError("sink must be a non-empty identifier.")
            result = self._compute_preview(clean_sink, active_policy, since=since, until=until)
        except Exception:
            self._observe_duration(active_policy.flush_cadence, OUTCOME_ERROR, started_mono)
            raise
        self._observe_duration(active_policy.flush_cadence, OUTCOME_OK, started_mono)
        return result

    def stage_rollup(
        self,
        *,
        sink: str,
        policy: RollupPolicy | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        skip_unchanged: bool = True,
    ) -> list[OutboxItem]:
        """Compute rollup preview and stage actionable proposals into `OutboxStore`."""
        preview = self.preview_rollup(sink=sink, policy=policy, since=since, until=until)
        actionable: list[RollupProposal] = []
        for proposal, diff in zip(preview.proposals, preview.diffs, strict=True):
            if skip_unchanged and diff.change_kind == CHANGE_KIND_UNCHANGED:
                continue
            actionable.append(proposal)
        return self._outbox_store.stage_proposals(actionable)

    def _compute_preview(
        self,
        sink: str,
        policy: RollupPolicy,
        *,
        since: datetime | None,
        until: datetime | None,
    ) -> RollupPreviewResult:
        """Group closed intervals, apply rounding/redaction, and diff against baselines."""
        activities = {act.id: act for act in self._ledger.list_activities(include_archived=True)}
        closed_intervals = self._ledger.list_intervals(
            since=since,
            until=until,
            include_open=False,
        )
        buckets, unmapped_count = _accumulate_buckets(closed_intervals, activities, policy)
        proposals, short_count = _finalize_proposals(sink, buckets, policy)
        baselines = self._outbox_store.list_flushed_bucket_baselines(sink)
        diffs = tuple(_build_preview_diff(prop, baselines) for prop in proposals)
        return RollupPreviewResult(
            proposals=proposals,
            diffs=diffs,
            cadence=policy.flush_cadence,
            grouping_window=policy.grouping_window,
            total_intervals_examined=len(closed_intervals),
            unmapped_interval_count=unmapped_count,
            filtered_short_bucket_count=short_count,
        )

    def _observe_duration(self, cadence: str, outcome: str, started_mono: float) -> None:
        """Record `worktrack_rollup_compute_duration_seconds` when telemetry is configured."""
        if self._telemetry is not None:
            elapsed = max(0.0, time.perf_counter() - started_mono)
            self._telemetry.observe_rollup_duration(
                cadence=cadence,
                outcome=outcome,
                duration_seconds=elapsed,
            )


def _accumulate_buckets(
    intervals: Sequence[Interval],
    activities: Mapping[str, Activity],
    policy: RollupPolicy,
) -> tuple[list[_BucketAccumulator], int]:
    """Group mapped closed `Intervals` into deterministic `(target_ref, window)` buckets."""
    grouped: dict[tuple[str, str, str, str], _BucketAccumulator] = {}
    window_cache: dict[tuple[int, int, int], tuple[datetime, datetime, str, str]] = {}
    target_cache = _precompute_activity_targets(activities, policy.standing_activity_mappings)
    unmapped = 0

    for interval in intervals:
        act_id = interval.activity_id
        target_ref = None if act_id is None else target_cache.get(act_id)
        if act_id is None or target_ref is None or interval.ended_at is None:
            unmapped += 1
            continue
        win_start, win_end, start_iso, end_iso, disambig = _resolve_cached_window(
            interval, policy.grouping_window, window_cache
        )
        bucket_key = (target_ref, start_iso, end_iso, disambig)
        acc = grouped.get(bucket_key)
        if acc is None:
            acc = _BucketAccumulator(
                target_ref=target_ref,
                activity_name=activities[act_id].name,
                window_start=win_start,
                window_end=win_end,
                window_start_iso=start_iso,
                window_end_iso=end_iso,
            )
            grouped[bucket_key] = acc
        acc.raw_duration_seconds += (interval.ended_at - interval.started_at).total_seconds()
        acc.interval_ids.append(interval.id)
        if not policy.redact_notes and interval.notes and interval.notes.strip():
            acc.notes.append(interval.notes.strip())

    ordered = sorted(
        grouped.values(),
        key=lambda item: (item.window_start_iso, item.target_ref, item.interval_ids[0]),
    )
    return ordered, unmapped


def _precompute_activity_targets(
    activities: Mapping[str, Activity],
    standing_mappings: Mapping[str, str],
) -> dict[str, str]:
    """Precompute outbound `target_ref` per `Activity.id` across the corpus."""
    resolved: dict[str, str] = {}
    for act_id, activity in activities.items():
        mapped = standing_mappings.get(activity.id) or standing_mappings.get(activity.name)
        if activity.kind == ACTIVITY_KIND_STANDING:
            target = mapped or activity.rollup_target_ref or activity.external_ref
        else:
            target = activity.rollup_target_ref or activity.external_ref or mapped
        if target is not None:
            resolved[act_id] = target
    return resolved


def _resolve_cached_window(
    interval: Interval,
    grouping_window: str,
    cache: dict[tuple[int, int, int], tuple[datetime, datetime, str, str]],
) -> tuple[datetime, datetime, str, str, str]:
    """Resolve `(window_start, window_end, start_iso, end_iso, disambiguator)` with date caching."""
    start = interval.started_at
    assert interval.ended_at is not None
    if grouping_window == GROUPING_WINDOW_INTERVAL:
        utc_s = start.astimezone(UTC)
        utc_e = interval.ended_at.astimezone(UTC)
        return utc_s, utc_e, format_utc_iso(utc_s), format_utc_iso(utc_e), interval.id
    ymd = (start.year, start.month, start.day)
    cached = cache.get(ymd)
    if cached is not None:
        return cached[0], cached[1], cached[2], cached[3], ""
    win_start, win_end = _compute_calendar_window(ymd[0], ymd[1], ymd[2], grouping_window)
    entry = (win_start, win_end, format_utc_iso(win_start), format_utc_iso(win_end))
    cache[ymd] = entry
    return entry[0], entry[1], entry[2], entry[3], ""


def _compute_calendar_window(
    year: int,
    month: int,
    day: int,
    grouping_window: str,
) -> tuple[datetime, datetime]:
    """Compute UTC `[window_start, window_end)` for `day`, `week`, or `month`."""
    day_start = datetime(year, month, day, tzinfo=UTC)
    if grouping_window == GROUPING_WINDOW_DAY:
        return day_start, day_start + timedelta(days=1)
    if grouping_window == GROUPING_WINDOW_WEEK:
        week_start = day_start - timedelta(days=day_start.weekday())
        return week_start, week_start + timedelta(days=DAYS_PER_WEEK)
    assert grouping_window == GROUPING_WINDOW_MONTH
    month_start = datetime(year, month, FIRST_DAY_OF_MONTH, tzinfo=UTC)
    if month == DECEMBER_MONTH:
        month_end = datetime(year + 1, JANUARY_MONTH, FIRST_DAY_OF_MONTH, tzinfo=UTC)
    else:
        month_end = datetime(year, month + 1, FIRST_DAY_OF_MONTH, tzinfo=UTC)
    return month_start, month_end


def _finalize_proposals(
    sink: str,
    buckets: Sequence[_BucketAccumulator],
    policy: RollupPolicy,
) -> tuple[tuple[RollupProposal, ...], int]:
    """Filter short buckets, round durations, redact notes, and build `RollupProposals`."""
    proposals: list[RollupProposal] = []
    filtered_short = 0
    for bucket in buckets:
        if bucket.raw_duration_seconds < policy.min_duration_seconds:
            filtered_short += 1
            continue
        rounded = round_duration_seconds(
            bucket.raw_duration_seconds,
            quantum_seconds=policy.rounding_quantum_seconds,
            mode=policy.rounding_mode,
        )
        if rounded <= 0 or rounded < policy.min_duration_seconds:
            filtered_short += 1
            continue
        proposals.append(_build_proposal_from_bucket(sink, bucket, rounded, policy))
    return tuple(proposals), filtered_short


def _build_proposal_from_bucket(
    sink: str,
    bucket: _BucketAccumulator,
    rounded: int,
    policy: RollupPolicy,
) -> RollupProposal:
    """Construct a validated `RollupProposal` and preview payload for `bucket`."""
    summary = _render_summary(bucket, policy)
    sorted_ids = tuple(sorted(bucket.interval_ids))
    material = {
        "sink": sink,
        "source_interval_ids": sorted_ids,
        "target_ref": bucket.target_ref,
        "window_end": bucket.window_end_iso,
        "window_start": bucket.window_start_iso,
    }
    idem_key = hashlib.sha256(
        json.dumps(material, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    preview_payload: dict[str, object] = {
        "sink": sink,
        "target_ref": bucket.target_ref,
        "started_at": bucket.window_start_iso,
        "window_end": bucket.window_end_iso,
        "time_spent_seconds": rounded,
        "summary": summary,
        "idempotency_key": idem_key,
    }
    return RollupProposal(
        sink=sink,
        target_ref=bucket.target_ref,
        window_start=bucket.window_start,
        window_end=bucket.window_end,
        raw_duration_seconds=bucket.raw_duration_seconds,
        rounded_duration_seconds=rounded,
        summary=summary,
        cadence=policy.flush_cadence,
        source_interval_ids=sorted_ids,
        idempotency_key=idem_key,
        payload_preview=preview_payload,
    )


def _render_summary(bucket: _BucketAccumulator, policy: RollupPolicy) -> str:
    """Format safe summary string honoring `policy.redact_notes`."""
    if policy.summary_template == DEFAULT_REDACTED_SUMMARY_TEMPLATE:
        base = bucket.activity_name
    else:
        base = policy.summary_template.format(
            activity_name=bucket.activity_name,
            target_ref=bucket.target_ref,
            window_start=bucket.window_start_iso[:ISO_DATE_LENGTH],
            window_end=bucket.window_end_iso[:ISO_DATE_LENGTH],
            interval_count=len(bucket.interval_ids),
        ).strip()
    if policy.redact_notes or not bucket.notes:
        return base or bucket.activity_name
    deduped_notes = list(dict.fromkeys(bucket.notes))
    joined_notes = NOTE_JOIN_SEPARATOR.join(deduped_notes)
    return f"{base}: {joined_notes}" if base else joined_notes


def _build_preview_diff(
    proposal: RollupProposal,
    baselines: Mapping[tuple[str, str, str], OutboxItem],
) -> PreviewDiff:
    """Compare `proposal` against previously flushed bucket baseline in `OutboxStore`."""
    start_iso = str(proposal.payload_preview["started_at"])
    end_iso = str(proposal.payload_preview["window_end"])
    prior = baselines.get((proposal.target_ref, start_iso, end_iso))
    if prior is None:
        return PreviewDiff(
            sink=proposal.sink,
            target_ref=proposal.target_ref,
            window_start=proposal.window_start,
            window_end=proposal.window_end,
            change_kind=CHANGE_KIND_CREATE,
            before_seconds=None,
            after_seconds=proposal.rounded_duration_seconds,
            delta_seconds=proposal.rounded_duration_seconds,
            before_summary=None,
            after_summary=proposal.summary,
            idempotency_key=proposal.idempotency_key,
        )
    delta = proposal.rounded_duration_seconds - prior.rounded_duration_seconds
    is_same = (
        prior.idempotency_key == proposal.idempotency_key
        and delta == 0
        and prior.summary == proposal.summary
    )
    kind = CHANGE_KIND_UNCHANGED if is_same else CHANGE_KIND_UPDATE
    return PreviewDiff(
        sink=proposal.sink,
        target_ref=proposal.target_ref,
        window_start=proposal.window_start,
        window_end=proposal.window_end,
        change_kind=kind,
        before_seconds=prior.rounded_duration_seconds,
        after_seconds=proposal.rounded_duration_seconds,
        delta_seconds=delta,
        before_summary=prior.summary,
        after_summary=proposal.summary,
        idempotency_key=proposal.idempotency_key,
        previous_outbox_item_id=prior.id,
    )


# endregion
