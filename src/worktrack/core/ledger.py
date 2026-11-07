"""Unified SQLite `Ledger` facade with optional Prometheus telemetry recording."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Self

from worktrack.config import WorktrackSettings
from worktrack.core import activities as activity_ops
from worktrack.core import chaotic as chaotic_ops
from worktrack.core import intervals as interval_ops
from worktrack.core.chaotic import RawIntervalSlice
from worktrack.core.ledger_mixins import EntityAndFacetLedgerMixin
from worktrack.core.models import (
    ACTIVITY_KIND_TARGETED,
    CAPTURE_MODE_CHAOTIC,
    CAPTURE_MODE_STRUCTURED,
    DEFAULT_ACTIVITY_SOURCE,
    OVERLAP_POLICY_FORBID,
    UNSET,
    Activity,
    Interval,
    UnsetType,
)
from worktrack.telemetry.metrics import TelemetryRecorder


class Ledger(EntityAndFacetLedgerMixin):
    """Anti-surveillance SQLite domain ledger for Worktrack Node persistence."""

    # region Lifecycle
    @classmethod
    def from_settings(
        cls,
        settings: WorktrackSettings,
        *,
        telemetry: TelemetryRecorder | None = None,
        busy_timeout_ms: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Construct a migrated `Ledger` bound to `settings.db_path`."""
        return cls(
            settings.db_path,
            telemetry=telemetry,
            busy_timeout_ms=busy_timeout_ms,
            environ=environ,
        )

    # endregion

    # region Activities
    def create_activity(
        self,
        name: str,
        *,
        kind: str = ACTIVITY_KIND_TARGETED,
        external_ref: str | None = None,
        rollup_target_ref: str | None = None,
        source: str = DEFAULT_ACTIVITY_SOURCE,
        tags: Sequence[str] = (),
        archived: bool = False,
        activity_id: str | None = None,
    ) -> Activity:
        """Create a *Targeted* or *Standing* `Activity`."""
        return activity_ops.create_activity(
            self._require_conn(),
            name,
            kind=kind,
            external_ref=external_ref,
            rollup_target_ref=rollup_target_ref,
            source=source,
            tags=tags,
            archived=archived,
            activity_id=activity_id,
        )

    def get_activity(self, activity_id: str) -> Activity:
        """Fetch an `Activity` by identifier."""
        return activity_ops.get_activity(self._require_conn(), activity_id)

    def find_activity_by_external_ref(
        self,
        external_ref: str,
        *,
        source: str | None = None,
    ) -> Activity | None:
        """Look up an `Activity` by external reference and optional source."""
        return activity_ops.find_activity_by_external_ref(
            self._require_conn(), external_ref, source=source
        )

    def update_activity(
        self,
        activity_id: str,
        *,
        name: str | None = None,
        external_ref: str | UnsetType | None = UNSET,
        rollup_target_ref: str | UnsetType | None = UNSET,
        archived: bool | None = None,
        tags: Sequence[str] | None = None,
    ) -> Activity:
        """Update mutable fields or freeform tags on an existing `Activity`."""
        return activity_ops.update_activity(
            self._require_conn(),
            activity_id,
            name=name,
            external_ref=external_ref,
            rollup_target_ref=rollup_target_ref,
            archived=archived,
            tags=tags,
        )

    def list_activities(
        self,
        *,
        kind: str | None = None,
        include_archived: bool = False,
        tag: str | None = None,
        tags: Sequence[str] | None = None,
        source: str | None = None,
    ) -> list[Activity]:
        """List `Activities` matching optional kind, archival, source, and tag filters."""
        return activity_ops.list_activities(
            self._require_conn(),
            kind=kind,
            include_archived=include_archived,
            tag=tag,
            tags=tags,
            source=source,
        )

    # endregion

    # region Intervals
    def clock_in(
        self,
        activity_id: str,
        *,
        started_at: datetime | None = None,
        notes: str | None = None,
        tags: Sequence[str] = (),
        overlap_policy: str = OVERLAP_POLICY_FORBID,
    ) -> Interval:
        """Start an active structured `Interval` and record structured telemetry."""
        interval = interval_ops.clock_in(
            self._require_conn(),
            activity_id,
            started_at=started_at,
            notes=notes,
            tags=tags,
            overlap_policy=overlap_policy,
        )
        if self._telemetry is not None:
            self._telemetry.record_interval(mode=CAPTURE_MODE_STRUCTURED)
        return interval

    def clock_out(
        self,
        *,
        ended_at: datetime | None = None,
        interval_id: str | None = None,
        notes: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> Interval:
        """Close the active open `Interval` (or explicit `interval_id`)."""
        return interval_ops.clock_out(
            self._require_conn(),
            ended_at=ended_at,
            interval_id=interval_id,
            notes=notes,
            tags=tags,
        )

    def record_structured_interval(
        self,
        activity_id: str,
        *,
        started_at: datetime,
        ended_at: datetime,
        notes: str | None = None,
        tags: Sequence[str] = (),
    ) -> Interval:
        """Record a closed structured `Interval` and increment structured telemetry."""
        interval = interval_ops.record_structured_interval(
            self._require_conn(),
            activity_id,
            started_at=started_at,
            ended_at=ended_at,
            notes=notes,
            tags=tags,
        )
        if self._telemetry is not None:
            self._telemetry.record_interval(mode=CAPTURE_MODE_STRUCTURED)
        return interval

    def record_chaotic_interval(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
        notes: str | None = None,
        tags: Sequence[str] = (),
        activity_id: str | None = None,
    ) -> Interval:
        """Record a chaotic `[started_at, ended_at)` block and increment chaotic metric."""
        interval = chaotic_ops.record_chaotic_interval(
            self._require_conn(),
            started_at=started_at,
            ended_at=ended_at,
            notes=notes,
            tags=tags,
            activity_id=activity_id,
        )
        if self._telemetry is not None:
            self._telemetry.record_interval(mode=CAPTURE_MODE_CHAOTIC)
        return interval

    def bind_interval(
        self,
        interval_id: str,
        activity_id: str,
        *,
        notes: str | None = None,
        tags: Sequence[str] | None = None,
        allow_rebind: bool = False,
    ) -> Interval:
        """Bind an unallocated `Interval` to a target `Activity`."""
        return chaotic_ops.bind_interval(
            self._require_conn(),
            interval_id,
            activity_id,
            notes=notes,
            tags=tags,
            allow_rebind=allow_rebind,
        )

    def split_interval(
        self,
        interval_id: str,
        slices: Sequence[RawIntervalSlice],
    ) -> list[Interval]:
        """Atomically split a closed `Interval` across multiple contiguous slices."""
        return chaotic_ops.split_interval(self._require_conn(), interval_id, slices)

    def get_interval(self, interval_id: str) -> Interval:
        """Fetch an `Interval` by identifier."""
        return interval_ops.get_interval(self._require_conn(), interval_id)

    def get_open_intervals(self) -> list[Interval]:
        """Return all currently open `Intervals` ordered chronologically."""
        return interval_ops.get_open_intervals(self._require_conn())

    def list_intervals(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        activity_id: str | None = None,
        capture_mode: str | None = None,
        include_open: bool = True,
        unallocated_only: bool = False,
        tag: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> list[Interval]:
        """Query `Intervals` matching `[since, until)` and optional attribute filters."""
        return interval_ops.list_intervals(
            self._require_conn(),
            since=since,
            until=until,
            activity_id=activity_id,
            capture_mode=capture_mode,
            include_open=include_open,
            unallocated_only=unallocated_only,
            tag=tag,
            tags=tags,
        )

    # endregion
