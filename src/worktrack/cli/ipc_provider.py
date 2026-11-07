"""Thread-safe `Ledger`- and `AgendaPlanner`-backed `worktrackd` IPC service provider."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from worktrack.agenda.models import ZERO_SECONDS_INT, AgendaError
from worktrack.agenda.planner import AgendaPlanner
from worktrack.agenda.validators import activity_display_key
from worktrack.cli.context import resolve_or_create_activity
from worktrack.config import WorktrackSettings
from worktrack.core.exceptions import LedgerError, LedgerValidationError
from worktrack.core.ledger import Ledger
from worktrack.core.models import (
    BUILTIN_WORKPLACE_FACET_KEY,
    OVERLAP_POLICY_FORBID,
    format_utc_iso,
)
from worktrack.core.schema import MEMORY_DB_SENTINEL
from worktrack.ipc.models import (
    AgendaTodaySnapshot,
    ClockInRequest,
    ClockOutRequest,
    DaemonStatusSnapshot,
    IpcApplicationError,
    OutboxSummarySnapshot,
)
from worktrack.outbox.exceptions import OutboxError
from worktrack.outbox.models import (
    EGRESS_OUTCOME_OK,
    OUTBOX_STATUS_APPROVED,
    OUTBOX_STATUS_STAGED,
)
from worktrack.outbox.store import OutboxStore
from worktrack.telemetry.metrics import TelemetryRecorder


class LedgerIpcServiceProvider:
    """Thread-safe `IpcServiceProvider` backed by SQLite `Ledger` and `AgendaPlanner`."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        telemetry: TelemetryRecorder | None = None,
        overlap_policy: str = OVERLAP_POLICY_FORBID,
        outbox_summary_fn: Callable[[], OutboxSummarySnapshot] | None = None,
        clock_fn: Callable[[], datetime] | None = None,
        busy_timeout_ms: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize provider and eagerly verify/migrate file-backed SQLite database."""
        raw_target = str(db_path).strip()
        if not raw_target or raw_target == MEMORY_DB_SENTINEL:
            raise LedgerValidationError(
                "LedgerIpcServiceProvider requires a filesystem SQLite path for thread safety."
            )
        self._db_path = Path(raw_target)
        self._telemetry = telemetry
        self._overlap_policy = overlap_policy
        self._outbox_summary_fn = outbox_summary_fn
        self._clock_fn = clock_fn
        self._busy_timeout_ms = busy_timeout_ms
        self._environ = environ
        self._lock = threading.RLock()

        # Eagerly apply core, agenda, and outbox migrations so worker threads attach cleanly
        with self._open_pair(), self._open_outbox_store():
            pass

    @classmethod
    def from_settings(
        cls,
        settings: WorktrackSettings,
        *,
        telemetry: TelemetryRecorder | None = None,
        overlap_policy: str = OVERLAP_POLICY_FORBID,
        outbox_summary_fn: Callable[[], OutboxSummarySnapshot] | None = None,
        clock_fn: Callable[[], datetime] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Construct a `LedgerIpcServiceProvider` from `WorktrackSettings`."""
        return cls(
            settings.db_path,
            telemetry=telemetry,
            overlap_policy=overlap_policy,
            outbox_summary_fn=outbox_summary_fn,
            clock_fn=clock_fn,
            environ=environ,
        )

    def status(self) -> DaemonStatusSnapshot:
        """Return active `Interval` and daily total snapshot from SQLite."""
        with self._lock:
            try:
                with self._open_pair() as (ledger, planner):
                    return build_daemon_status_snapshot(ledger, planner, now=self._now())
            except (LedgerError, AgendaError) as exc:
                raise IpcApplicationError(str(exc)) from exc

    def clock_in(self, request: ClockInRequest) -> DaemonStatusSnapshot:
        """Resolve or create `Activity`, start structured `Interval`, and return status."""
        with self._lock:
            try:
                now = self._now()
                with self._open_pair() as (ledger, planner):
                    activity = resolve_or_create_activity(
                        ledger,
                        request.activity_key,
                        kind=request.activity_kind,
                        title=request.activity_title,
                    )
                    ledger.clock_in(
                        activity.id,
                        started_at=now,
                        notes=request.note,
                        overlap_policy=self._overlap_policy,
                    )
                    if request.workplace is not None:
                        ledger.assert_context_fact(
                            BUILTIN_WORKPLACE_FACET_KEY,
                            request.workplace,
                            scope_date=now.date(),
                        )
                    return build_daemon_status_snapshot(ledger, planner, now=now)
            except (LedgerError, AgendaError) as exc:
                raise IpcApplicationError(str(exc)) from exc

    def clock_out(self, request: ClockOutRequest) -> DaemonStatusSnapshot:
        """Close the active open `Interval` and return updated status snapshot."""
        with self._lock:
            try:
                now = self._now()
                with self._open_pair() as (ledger, planner):
                    ledger.clock_out(ended_at=now, notes=request.note)
                    return build_daemon_status_snapshot(ledger, planner, now=now)
            except (LedgerError, AgendaError) as exc:
                raise IpcApplicationError(str(exc)) from exc

    def agenda_today(self) -> AgendaTodaySnapshot:
        """Return prioritized Today `Agenda` items and scheduled `FocusBlocks`."""
        with self._lock:
            try:
                with self._open_pair() as (ledger, planner):
                    return planner.to_ipc_today_snapshot(now=self._now(), ledger=ledger)
            except (LedgerError, AgendaError) as exc:
                raise IpcApplicationError(str(exc)) from exc

    def outbox_summary(self) -> OutboxSummarySnapshot:
        """Return staged `Outbox` summary via callback or live `OutboxStore` query."""
        with self._lock:
            try:
                if self._outbox_summary_fn is not None:
                    return self._outbox_summary_fn()
                with self._open_outbox_store() as store:
                    return build_outbox_summary_snapshot(store)
            except (LedgerError, AgendaError, OutboxError) as exc:
                raise IpcApplicationError(str(exc)) from exc

    def _now(self) -> datetime:
        """Return current UTC timestamp from injected `clock_fn` or wall clock."""
        return datetime.now(UTC) if self._clock_fn is None else self._clock_fn()

    @contextmanager
    def _open_outbox_store(self) -> Iterator[OutboxStore]:
        """Open and deterministically close `OutboxStore` for a single RPC."""
        with OutboxStore(
            self._db_path,
            telemetry=self._telemetry,
            busy_timeout_ms=self._busy_timeout_ms,
            environ=self._environ,
        ) as store:
            yield store

    @contextmanager
    def _open_pair(self) -> Iterator[tuple[Ledger, AgendaPlanner]]:
        """Open and deterministically close `(Ledger, AgendaPlanner)` for a single RPC."""
        with (
            Ledger(
                self._db_path,
                telemetry=self._telemetry,
                busy_timeout_ms=self._busy_timeout_ms,
                environ=self._environ,
            ) as ledger,
            AgendaPlanner(
                self._db_path,
                ledger=ledger,
                telemetry=self._telemetry,
                busy_timeout_ms=self._busy_timeout_ms,
                environ=self._environ,
            ) as planner,
        ):
            yield ledger, planner


def build_daemon_status_snapshot(
    ledger: Ledger,
    planner: AgendaPlanner,
    *,
    now: datetime | None = None,
) -> DaemonStatusSnapshot:
    """Compute a `DaemonStatusSnapshot` from `Ledger` and `AgendaPlanner`."""
    insight = planner.get_now_insight(now=now, ledger=ledger)
    if insight.active_interval is not None and insight.active_activity is not None:
        act = insight.active_activity
        return DaemonStatusSnapshot(
            active_activity_key=activity_display_key(act),
            active_activity_title=act.name,
            active_activity_kind=act.kind,
            active_interval_elapsed_seconds=insight.active_elapsed_seconds,
            daily_total_seconds=insight.today_total_seconds,
            active_interval_started_at=format_utc_iso(insight.active_interval.started_at),
            workplace=insight.workplace,
        )
    return DaemonStatusSnapshot(
        active_interval_elapsed_seconds=ZERO_SECONDS_INT,
        daily_total_seconds=insight.today_total_seconds,
        workplace=insight.workplace,
    )


def build_outbox_summary_snapshot(store: OutboxStore) -> OutboxSummarySnapshot:
    """Compute a live `OutboxSummarySnapshot` from SQLite `OutboxStore`."""
    staged = store.list_items(status=OUTBOX_STATUS_STAGED)
    approved = store.list_items(status=OUTBOX_STATUS_APPROVED)
    pending = [*staged, *approved]
    pending_seconds = sum(item.rounded_duration_seconds for item in pending)
    distinct_sinks = tuple(sorted({item.sink for item in pending}))
    ok_records = store.list_egress_records(outcome=EGRESS_OUTCOME_OK)
    last_flush = format_utc_iso(ok_records[-1].recorded_at) if ok_records else None
    return OutboxSummarySnapshot(
        staged_count=len(staged),
        pending_seconds=pending_seconds,
        sinks=distinct_sinks,
        last_flush_at=last_flush,
    )
