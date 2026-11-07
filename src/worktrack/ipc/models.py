"""Typed IPC domain snapshots, exceptions, and injectable `StatusProvider` protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

# region Constants
DEFAULT_ACTIVITY_KIND = "targeted"
ALLOWED_ACTIVITY_KINDS: frozenset[str] = frozenset({"targeted", "standing"})
MIN_NON_NEGATIVE_COUNT = 0
DEFAULT_APPLICATION_ERROR_CODE = -32000
# endregion


# region Exceptions
class IpcError(Exception):
    """Base exception for Worktrack IPC transport, framing, auth, and RPC failures."""


class IpcTransportError(IpcError):
    """Raised when IPC socket bind, connect, framing, or I/O timeout fails."""


class IpcAuthenticationError(IpcError):
    """Raised when loopback TCP bearer token configuration or verification fails."""


class IpcRpcError(IpcError):
    """Raised when `worktrackd` returns a structured JSON-RPC 2.0 error object.

    Attributes:
        code: Numeric JSON-RPC 2.0 error code.
        message: Human-readable error summary.
        data: Optional structured diagnostic payload.
    """

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        """Initialize JSON-RPC error with `code`, `message`, and optional `data`."""
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class IpcApplicationError(IpcError):
    """Domain error raised by `StatusProvider` implementations with a custom RPC code."""

    def __init__(self, message: str, *, code: int = DEFAULT_APPLICATION_ERROR_CODE) -> None:
        """Initialize domain RPC error with `message` and JSON-RPC `code`."""
        super().__init__(message)
        self.code = code
        self.message = message


# endregion


# region Models
def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    """Extract an optional stripped non-empty string field from a mapping."""
    raw = data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"Field '{key}' must be a string or null.")
    stripped = raw.strip()
    return stripped if stripped else None


def _required_str(data: Mapping[str, object], key: str) -> str:
    """Extract a required non-empty string field from a mapping."""
    value = _optional_str(data, key)
    if value is None:
        raise ValueError(f"Field '{key}' must be a non-empty string.")
    return value


def _non_negative_int(data: Mapping[str, object], key: str) -> int:
    """Extract an optional non-negative integer field defaulting to zero."""
    raw = data.get(key, MIN_NON_NEGATIVE_COUNT)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < MIN_NON_NEGATIVE_COUNT:
        raise ValueError(f"Field '{key}' must be a non-negative integer.")
    return raw


@dataclass(frozen=True, slots=True)
class DaemonStatusSnapshot:
    """Point-in-time `worktrackd` status snapshot for CLI and shell prompt consumers.

    Attributes:
        active_activity_key: Identifier of currently tracked `Activity` (`PROJ-142`), if any.
        active_activity_title: Human-readable title of active `Activity`, if any.
        active_activity_kind: `targeted` or `standing` when tracking, else `None`.
        active_interval_elapsed_seconds: Elapsed seconds in the active `Interval`.
        daily_total_seconds: Cumulative tracked seconds today across all `Intervals`.
        active_interval_started_at: Optional ISO-8601 UTC start timestamp of open `Interval`.
        workplace: Optional active `workplace` `ContextFact` (`home_office`, `office`, etc.).
    """

    active_activity_key: str | None = None
    active_activity_title: str | None = None
    active_activity_kind: str | None = None
    active_interval_elapsed_seconds: int = MIN_NON_NEGATIVE_COUNT
    daily_total_seconds: int = MIN_NON_NEGATIVE_COUNT
    active_interval_started_at: str | None = None
    workplace: str | None = None

    @property
    def is_tracking(self) -> bool:
        """Return `True` when an open `Interval` is currently bound to an `Activity`."""
        return bool(self.active_activity_key or self.active_activity_title)

    def to_dict(self) -> dict[str, object]:
        """Serialize snapshot into a JSON-compatible dictionary."""
        return {
            "active_activity_key": self.active_activity_key,
            "active_activity_title": self.active_activity_title,
            "active_activity_kind": self.active_activity_kind,
            "active_interval_elapsed_seconds": self.active_interval_elapsed_seconds,
            "daily_total_seconds": self.daily_total_seconds,
            "active_interval_started_at": self.active_interval_started_at,
            "workplace": self.workplace,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> DaemonStatusSnapshot:
        """Deserialize and validate a `DaemonStatusSnapshot` from a mapping."""
        kind = _optional_str(data, "active_activity_kind")
        if kind is not None and kind not in ALLOWED_ACTIVITY_KINDS:
            raise ValueError(f"Invalid active_activity_kind: '{kind}'.")
        return cls(
            active_activity_key=_optional_str(data, "active_activity_key"),
            active_activity_title=_optional_str(data, "active_activity_title"),
            active_activity_kind=kind,
            active_interval_elapsed_seconds=_non_negative_int(
                data, "active_interval_elapsed_seconds"
            ),
            daily_total_seconds=_non_negative_int(data, "daily_total_seconds"),
            active_interval_started_at=_optional_str(data, "active_interval_started_at"),
            workplace=_optional_str(data, "workplace"),
        )


@dataclass(frozen=True, slots=True)
class ClockInRequest:
    """Parameters for starting a structured `Interval` via `clock_in` IPC."""

    activity_key: str
    activity_title: str | None = None
    activity_kind: str = DEFAULT_ACTIVITY_KIND
    note: str | None = None
    workplace: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize `ClockInRequest` into JSON-RPC `params` dictionary."""
        return {
            "activity_key": self.activity_key,
            "activity_title": self.activity_title,
            "activity_kind": self.activity_kind,
            "note": self.note,
            "workplace": self.workplace,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ClockInRequest:
        """Parse and validate `ClockInRequest` from JSON-RPC `params`."""
        activity_key = _required_str(data, "activity_key")
        kind = _optional_str(data, "activity_kind") or DEFAULT_ACTIVITY_KIND
        if kind not in ALLOWED_ACTIVITY_KINDS:
            raise ValueError(f"activity_kind must be one of {sorted(ALLOWED_ACTIVITY_KINDS)}.")
        return cls(
            activity_key=activity_key,
            activity_title=_optional_str(data, "activity_title"),
            activity_kind=kind,
            note=_optional_str(data, "note"),
            workplace=_optional_str(data, "workplace"),
        )


@dataclass(frozen=True, slots=True)
class ClockOutRequest:
    """Parameters for closing the active `Interval` via `clock_out` IPC."""

    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize `ClockOutRequest` into JSON-RPC `params` dictionary."""
        return {"note": self.note}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ClockOutRequest:
        """Parse and validate `ClockOutRequest` from JSON-RPC `params`."""
        return cls(note=_optional_str(data, "note"))


@dataclass(frozen=True, slots=True)
class AgendaItemSummary:
    """Summary of a prioritized Today `Agenda` item."""

    activity_key: str
    title: str
    budget_seconds: int = MIN_NON_NEGATIVE_COUNT
    logged_seconds: int = MIN_NON_NEGATIVE_COUNT
    deadline: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize `AgendaItemSummary` into a dictionary."""
        return {
            "activity_key": self.activity_key,
            "title": self.title,
            "budget_seconds": self.budget_seconds,
            "logged_seconds": self.logged_seconds,
            "deadline": self.deadline,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> AgendaItemSummary:
        """Deserialize `AgendaItemSummary` from a mapping."""
        return cls(
            activity_key=_required_str(data, "activity_key"),
            title=_required_str(data, "title"),
            budget_seconds=_non_negative_int(data, "budget_seconds"),
            logged_seconds=_non_negative_int(data, "logged_seconds"),
            deadline=_optional_str(data, "deadline"),
        )


@dataclass(frozen=True, slots=True)
class FocusBlockSummary:
    """Summary of a scheduled `FocusBlock` window for Today."""

    block_id: str
    activity_key: str
    start_at: str
    end_at: str

    def to_dict(self) -> dict[str, object]:
        """Serialize `FocusBlockSummary` into a dictionary."""
        return {
            "block_id": self.block_id,
            "activity_key": self.activity_key,
            "start_at": self.start_at,
            "end_at": self.end_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FocusBlockSummary:
        """Deserialize `FocusBlockSummary` from a mapping."""
        return cls(
            block_id=_required_str(data, "block_id"),
            activity_key=_required_str(data, "activity_key"),
            start_at=_required_str(data, "start_at"),
            end_at=_required_str(data, "end_at"),
        )


@dataclass(frozen=True, slots=True)
class AgendaTodaySnapshot:
    """Snapshot of prioritized Today `Agenda` items and scheduled `FocusBlocks`."""

    date_iso: str
    items: tuple[AgendaItemSummary, ...] = ()
    focus_blocks: tuple[FocusBlockSummary, ...] = ()
    planned_budget_seconds: int = MIN_NON_NEGATIVE_COUNT
    logged_total_seconds: int = MIN_NON_NEGATIVE_COUNT
    workplace: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize `AgendaTodaySnapshot` into a JSON-compatible dictionary."""
        return {
            "date_iso": self.date_iso,
            "items": [item.to_dict() for item in self.items],
            "focus_blocks": [block.to_dict() for block in self.focus_blocks],
            "planned_budget_seconds": self.planned_budget_seconds,
            "logged_total_seconds": self.logged_total_seconds,
            "workplace": self.workplace,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> AgendaTodaySnapshot:
        """Deserialize `AgendaTodaySnapshot` from a mapping."""
        raw_items = data.get("items", [])
        raw_blocks = data.get("focus_blocks", [])
        if not isinstance(raw_items, list) or not isinstance(raw_blocks, list):
            raise ValueError("'items' and 'focus_blocks' must be lists.")
        items = tuple(
            AgendaItemSummary.from_dict(entry) for entry in raw_items if isinstance(entry, Mapping)
        )
        blocks = tuple(
            FocusBlockSummary.from_dict(entry) for entry in raw_blocks if isinstance(entry, Mapping)
        )
        return cls(
            date_iso=_required_str(data, "date_iso"),
            items=items,
            focus_blocks=blocks,
            planned_budget_seconds=_non_negative_int(data, "planned_budget_seconds"),
            logged_total_seconds=_non_negative_int(data, "logged_total_seconds"),
            workplace=_optional_str(data, "workplace"),
        )


@dataclass(frozen=True, slots=True)
class OutboxSummarySnapshot:
    """Summary of staged `Outbox` proposals awaiting human review or sink flush."""

    staged_count: int = MIN_NON_NEGATIVE_COUNT
    pending_seconds: int = MIN_NON_NEGATIVE_COUNT
    sinks: tuple[str, ...] = ()
    last_flush_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize `OutboxSummarySnapshot` into a JSON-compatible dictionary."""
        return {
            "staged_count": self.staged_count,
            "pending_seconds": self.pending_seconds,
            "sinks": list(self.sinks),
            "last_flush_at": self.last_flush_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> OutboxSummarySnapshot:
        """Deserialize `OutboxSummarySnapshot` from a mapping."""
        raw_sinks = data.get("sinks", [])
        if not isinstance(raw_sinks, list) or not all(isinstance(s, str) for s in raw_sinks):
            raise ValueError("'sinks' must be a list of sink identifier strings.")
        return cls(
            staged_count=_non_negative_int(data, "staged_count"),
            pending_seconds=_non_negative_int(data, "pending_seconds"),
            sinks=tuple(s.strip() for s in raw_sinks if s.strip()),
            last_flush_at=_optional_str(data, "last_flush_at"),
        )


# endregion


# region Protocols
@runtime_checkable
class StatusProvider(Protocol):
    """Injectable service contract bound by `worktrackd` to serve IPC methods."""

    def status(self) -> DaemonStatusSnapshot:
        """Return active `Interval` and daily total snapshot."""

    def clock_in(self, request: ClockInRequest) -> DaemonStatusSnapshot:
        """Start a structured `Interval` for an `Activity` and return updated status."""

    def clock_out(self, request: ClockOutRequest) -> DaemonStatusSnapshot:
        """Close the active `Interval` and return updated status."""

    def agenda_today(self) -> AgendaTodaySnapshot:
        """Return prioritized Today `Agenda` items and scheduled `FocusBlocks`."""

    def outbox_summary(self) -> OutboxSummarySnapshot:
        """Return staged `Outbox` counts and pending duration summary."""


IpcServiceProvider = StatusProvider


class StaticStatusProvider:
    """Lightweight in-memory `StatusProvider` for standalone IPC testing and warm probes."""

    def __init__(
        self,
        *,
        initial_status: DaemonStatusSnapshot | None = None,
        initial_agenda: AgendaTodaySnapshot | None = None,
        initial_outbox: OutboxSummarySnapshot | None = None,
    ) -> None:
        """Initialize provider with optional seed status, agenda, and outbox snapshots."""
        self._status = DaemonStatusSnapshot() if initial_status is None else initial_status
        self._agenda = (
            AgendaTodaySnapshot(date_iso="1970-01-01") if initial_agenda is None else initial_agenda
        )
        self._outbox = OutboxSummarySnapshot() if initial_outbox is None else initial_outbox

    def status(self) -> DaemonStatusSnapshot:
        """Return current in-memory `DaemonStatusSnapshot`."""
        return self._status

    def clock_in(self, request: ClockInRequest) -> DaemonStatusSnapshot:
        """Record active `Activity` in memory and return updated snapshot."""
        self._status = DaemonStatusSnapshot(
            active_activity_key=request.activity_key,
            active_activity_title=request.activity_title or request.activity_key,
            active_activity_kind=request.activity_kind,
            active_interval_elapsed_seconds=MIN_NON_NEGATIVE_COUNT,
            daily_total_seconds=self._status.daily_total_seconds,
            workplace=request.workplace or self._status.workplace,
        )
        return self._status

    def clock_out(self, request: ClockOutRequest) -> DaemonStatusSnapshot:
        """Clear active `Activity` in memory and return idle snapshot."""
        _ = request.note
        self._status = replace(
            self._status,
            active_activity_key=None,
            active_activity_title=None,
            active_activity_kind=None,
            active_interval_elapsed_seconds=MIN_NON_NEGATIVE_COUNT,
            active_interval_started_at=None,
        )
        return self._status

    def agenda_today(self) -> AgendaTodaySnapshot:
        """Return current in-memory `AgendaTodaySnapshot`."""
        return self._agenda

    def outbox_summary(self) -> OutboxSummarySnapshot:
        """Return current in-memory `OutboxSummarySnapshot`."""
        return self._outbox


# endregion
