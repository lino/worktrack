"""SQLite-backed `AgendaPlanner` facade for horizons, `FocusBlocks`, and insights."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from worktrack.agenda import insights as insight_ops
from worktrack.agenda import persistence as persistence_ops
from worktrack.agenda.models import (
    AGENDA_STATUS_DONE,
    AGENDA_STATUS_PLANNED,
    DEFAULT_AGENDA_PRIORITY,
    DEFAULT_APPROACHING_DEADLINE_HOURS,
    EMPTY_AGENDA_SCHEMA_VERSION,
    HORIZON_TODAY,
    METRIC_AGENDA_ITEMS_SCHEDULED_TOTAL,
    AgendaError,
    AgendaItem,
    AgendaPriority,
    AgendaValidationError,
    FocusBlock,
    HorizonInsight,
    NowInsight,
)
from worktrack.agenda.schema import apply_agenda_migrations, list_applied_agenda_migrations
from worktrack.config import WorktrackSettings
from worktrack.core.ledger import Ledger
from worktrack.core.models import UNSET, MigrationRecord, UnsetType
from worktrack.core.schema import (
    DEFAULT_DB_DIR_MODE,
    open_sqlite_connection,
    prepare_db_target,
    resolve_busy_timeout_ms,
)
from worktrack.ipc.models import AgendaTodaySnapshot
from worktrack.telemetry.metrics import TelemetryRecorder


class AgendaPlanner:
    """Horizon planner (`Now / Today / This Week`) and `FocusBlock` store."""

    # region Lifecycle
    def __init__(
        self,
        db_path: Path | str,
        *,
        ledger: Ledger | None = None,
        telemetry: TelemetryRecorder | None = None,
        busy_timeout_ms: int | None = None,
        dir_mode: int = DEFAULT_DB_DIR_MODE,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Open SQLite WAL database and apply `agenda_schema_migrations`."""
        resolved_timeout = resolve_busy_timeout_ms(busy_timeout_ms, environ=environ)
        connect_target, resolved_path = prepare_db_target(db_path, dir_mode=dir_mode)
        conn = open_sqlite_connection(connect_target, busy_timeout_ms=resolved_timeout)
        try:
            apply_agenda_migrations(conn)
        except Exception:
            conn.close()
            raise
        self._conn: sqlite3.Connection = conn
        self._db_path: Path | None = resolved_path
        self._busy_timeout_ms: int = resolved_timeout
        self._ledger: Ledger | None = ledger
        self._telemetry: TelemetryRecorder | None = telemetry
        self._closed: bool = False

    @classmethod
    def from_settings(
        cls,
        settings: WorktrackSettings,
        *,
        ledger: Ledger | None = None,
        telemetry: TelemetryRecorder | None = None,
        busy_timeout_ms: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Construct an `AgendaPlanner` bound to `settings.db_path`."""
        return cls(
            settings.db_path,
            ledger=ledger,
            telemetry=telemetry,
            busy_timeout_ms=busy_timeout_ms,
            environ=environ,
        )

    @property
    def db_path(self) -> Path | None:
        """Return resolved SQLite file `Path` (or `None` for `:memory:`)."""
        return self._db_path

    @property
    def agenda_schema_version(self) -> int:
        """Return highest applied `agenda_schema_migrations` version."""
        row = (
            self._require_conn()
            .execute("SELECT COALESCE(MAX(version), 0) FROM agenda_schema_migrations")
            .fetchone()
        )
        return EMPTY_AGENDA_SCHEMA_VERSION if row is None else int(row[0])

    def applied_agenda_migrations(self) -> list[MigrationRecord]:
        """Return ordered list of applied `agenda_schema_migrations` records."""
        return list_applied_agenda_migrations(self._require_conn())

    def close(self) -> None:
        """Close the underlying SQLite connection idempotently."""
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self) -> Self:
        """Enter context manager returning this `AgendaPlanner`."""
        self._require_conn()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the SQLite connection on context exit."""
        self.close()

    def _require_conn(self) -> sqlite3.Connection:
        """Return active SQLite connection or raise `AgendaError` if closed."""
        if self._closed:
            raise AgendaError("AgendaPlanner SQLite connection is closed.")
        return self._conn

    @contextmanager
    def _resolve_ledger(self, explicit_ledger: Ledger | None) -> Iterator[Ledger]:
        """Yield explicit/bound `Ledger` or open a scoped file-backed `Ledger`."""
        if explicit_ledger is not None:
            yield explicit_ledger
            return
        if self._ledger is not None:
            yield self._ledger
            return
        if self._db_path is None:
            raise AgendaValidationError(
                "In-memory AgendaPlanner requires an explicit Ledger instance for insights."
            )
        with Ledger(
            self._db_path,
            telemetry=self._telemetry,
            busy_timeout_ms=self._busy_timeout_ms,
        ) as scoped_ledger:
            yield scoped_ledger

    # endregion

    # region Agenda Items
    def schedule_item(
        self,
        activity_id: str,
        *,
        horizon: str = HORIZON_TODAY,
        priority: int | AgendaPriority = DEFAULT_AGENDA_PRIORITY,
        status: str = AGENDA_STATUS_PLANNED,
        budget_seconds: int | None = None,
        daily_budget_minutes: int | None = None,
        deadline: str | date | datetime | None = None,
        notes: str | None = None,
        item_id: str | None = None,
    ) -> AgendaItem:
        """Schedule an `AgendaItem` and increment `worktrack_agenda_items_scheduled_total`."""
        item = persistence_ops.insert_agenda_item(
            self._require_conn(),
            activity_id,
            horizon=horizon,
            priority=priority,
            status=status,
            budget_seconds=budget_seconds,
            daily_budget_minutes=daily_budget_minutes,
            deadline=deadline,
            notes=notes,
            item_id=item_id,
        )
        if self._telemetry is not None:
            self._telemetry.increment_counter(METRIC_AGENDA_ITEMS_SCHEDULED_TOTAL)
        return item

    def get_item(self, item_id: str) -> AgendaItem:
        """Fetch an `AgendaItem` by identifier."""
        return persistence_ops.get_agenda_item(self._require_conn(), item_id)

    def update_item(
        self,
        item_id: str,
        *,
        horizon: str | None = None,
        priority: int | AgendaPriority | None = None,
        status: str | None = None,
        budget_seconds: int | UnsetType | None = UNSET,
        daily_budget_minutes: int | UnsetType | None = UNSET,
        deadline: str | date | datetime | UnsetType | None = UNSET,
        notes: str | UnsetType | None = UNSET,
    ) -> AgendaItem:
        """Update mutable fields on an existing `AgendaItem`."""
        return persistence_ops.update_agenda_item(
            self._require_conn(),
            item_id,
            horizon=horizon,
            priority=priority,
            status=status,
            budget_seconds=budget_seconds,
            daily_budget_minutes=daily_budget_minutes,
            deadline=deadline,
            notes=notes,
        )

    def complete_item(self, item_id: str) -> AgendaItem:
        """Mark an `AgendaItem` as `done`."""
        return self.update_item(item_id, status=AGENDA_STATUS_DONE)

    def list_items(
        self,
        *,
        horizon: str | None = None,
        horizons: Sequence[str] | None = None,
        status: str | None = None,
        include_done: bool = True,
        activity_id: str | None = None,
    ) -> list[AgendaItem]:
        """List `AgendaItems` ordered by priority, deadline, and creation time."""
        return persistence_ops.list_agenda_items(
            self._require_conn(),
            horizon=horizon,
            horizons=horizons,
            status=status,
            include_done=include_done,
            activity_id=activity_id,
        )

    # endregion

    # region Focus Blocks
    def schedule_focus_block(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
        label: str | None = None,
        activity_id: str | None = None,
        expose_busy: bool = False,
        notes: str | None = None,
        block_id: str | None = None,
    ) -> FocusBlock:
        """Schedule a `FocusBlock` and increment `worktrack_agenda_items_scheduled_total`."""
        block = persistence_ops.insert_focus_block(
            self._require_conn(),
            started_at=started_at,
            ended_at=ended_at,
            label=label,
            activity_id=activity_id,
            expose_busy=expose_busy,
            notes=notes,
            block_id=block_id,
        )
        if self._telemetry is not None:
            self._telemetry.increment_counter(METRIC_AGENDA_ITEMS_SCHEDULED_TOTAL)
        return block

    def get_focus_block(self, block_id: str) -> FocusBlock:
        """Fetch a `FocusBlock` by identifier."""
        return persistence_ops.get_focus_block(self._require_conn(), block_id)

    def list_focus_blocks(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        activity_id: str | None = None,
    ) -> list[FocusBlock]:
        """List `FocusBlocks` intersecting optional `[since, until)` bounds."""
        return persistence_ops.list_focus_blocks(
            self._require_conn(),
            since=since,
            until=until,
            activity_id=activity_id,
        )

    def delete_focus_block(self, block_id: str) -> None:
        """Delete a scheduled `FocusBlock` by identifier."""
        persistence_ops.delete_focus_block(self._require_conn(), block_id)

    # endregion

    # region Insights
    def get_now_insight(
        self,
        *,
        now: datetime | None = None,
        ledger: Ledger | None = None,
        approaching_deadline_hours: int = DEFAULT_APPROACHING_DEADLINE_HOURS,
    ) -> NowInsight:
        """Compute `NowInsight` answering 'what am I doing right now?'."""
        with self._resolve_ledger(ledger) as active_ledger:
            return insight_ops.compute_now_insight(
                self._require_conn(),
                active_ledger,
                now=now,
                approaching_deadline_hours=approaching_deadline_hours,
            )

    def get_today_insight(
        self,
        *,
        now: datetime | None = None,
        ledger: Ledger | None = None,
        approaching_deadline_hours: int = DEFAULT_APPROACHING_DEADLINE_HOURS,
    ) -> HorizonInsight:
        """Compute `HorizonInsight` answering 'what am I doing today?'."""
        with self._resolve_ledger(ledger) as active_ledger:
            return insight_ops.compute_today_insight(
                self._require_conn(),
                active_ledger,
                now=now,
                approaching_deadline_hours=approaching_deadline_hours,
            )

    def get_week_insight(
        self,
        *,
        now: datetime | None = None,
        ledger: Ledger | None = None,
        approaching_deadline_hours: int = DEFAULT_APPROACHING_DEADLINE_HOURS,
    ) -> HorizonInsight:
        """Compute `HorizonInsight` answering 'what am I doing this week?'."""
        with self._resolve_ledger(ledger) as active_ledger:
            return insight_ops.compute_week_insight(
                self._require_conn(),
                active_ledger,
                now=now,
                approaching_deadline_hours=approaching_deadline_hours,
            )

    def to_ipc_today_snapshot(
        self,
        *,
        now: datetime | None = None,
        ledger: Ledger | None = None,
    ) -> AgendaTodaySnapshot:
        """Build wire-serializable `AgendaTodaySnapshot` for `worktrackd` IPC."""
        with self._resolve_ledger(ledger) as active_ledger:
            insight = insight_ops.compute_today_insight(
                self._require_conn(),
                active_ledger,
                now=now,
            )
            act_map = {act.id: act for act in active_ledger.list_activities(include_archived=True)}
            return insight_ops.build_ipc_today_snapshot(insight, act_map)

    # endregion
