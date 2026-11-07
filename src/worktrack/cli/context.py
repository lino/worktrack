"""Execution context, bounded telemetry labels, and shared resolvers for `wt` CLI."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, TextIO

from worktrack.agenda.planner import AgendaPlanner
from worktrack.config import WorktrackSettings
from worktrack.core.exceptions import ActivityNotFoundError, LedgerValidationError
from worktrack.core.ledger import Ledger
from worktrack.core.models import (
    ACTIVITY_KIND_TARGETED,
    FACET_TYPE_BOOL,
    FACET_TYPE_ENUM,
    FACET_TYPE_FLOAT,
    FACET_TYPE_INT,
    FACET_TYPE_STRING,
    UNSET,
    Activity,
    ContextFact,
    Facet,
    FacetValue,
    format_utc_iso,
)
from worktrack.outbox.store import OutboxStore
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
EXIT_SUCCESS: Final = 0
EXIT_ERROR: Final = 1
EXIT_USAGE_ERROR: Final = 2

SUBCOMMAND_IN: Final = "in"
SUBCOMMAND_OUT: Final = "out"
SUBCOMMAND_LOG: Final = "log"
SUBCOMMAND_SPLIT: Final = "split"
SUBCOMMAND_FACT: Final = "fact"
SUBCOMMAND_NOW: Final = "now"
SUBCOMMAND_TODAY: Final = "today"
SUBCOMMAND_WEEK: Final = "week"
SUBCOMMAND_AGENDA: Final = "agenda"
SUBCOMMAND_OUTBOX: Final = "outbox"
SUBCOMMAND_JIRA: Final = "jira"
SUBCOMMAND_SERVE: Final = "serve"
SUBCOMMAND_DAEMON: Final = "daemon"
SUBCOMMAND_STATUS: Final = "status"
SUBCOMMAND_METRICS: Final = "metrics"
SUBCOMMAND_HELP: Final = "help"
SUBCOMMAND_UNKNOWN: Final = "unknown"

CANONICAL_CLI_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        SUBCOMMAND_IN,
        SUBCOMMAND_OUT,
        SUBCOMMAND_LOG,
        SUBCOMMAND_SPLIT,
        SUBCOMMAND_FACT,
        SUBCOMMAND_NOW,
        SUBCOMMAND_TODAY,
        SUBCOMMAND_WEEK,
        SUBCOMMAND_AGENDA,
        SUBCOMMAND_OUTBOX,
        SUBCOMMAND_JIRA,
        SUBCOMMAND_SERVE,
        SUBCOMMAND_DAEMON,
        SUBCOMMAND_STATUS,
        SUBCOMMAND_METRICS,
        SUBCOMMAND_HELP,
        SUBCOMMAND_UNKNOWN,
    }
)

OUTCOME_OK: Final = "ok"
OUTCOME_ERROR: Final = "error"

BOOL_TRUE_LITERALS: Final[frozenset[str]] = frozenset({"true", "1", "yes", "on"})
BOOL_FALSE_LITERALS: Final[frozenset[str]] = frozenset({"false", "0", "no", "off"})
UNALLOCATED_SLICE_TOKENS: Final[frozenset[str]] = frozenset({"", "none", "unallocated", "-"})
HH_MM_COLON_LEN: Final = 5
# endregion


# region Models
@dataclass(slots=True)
class CliContext:
    """Injectable runtime state passed to every `wt` CLI subcommand handler."""

    settings: WorktrackSettings
    telemetry: TelemetryRecorder
    environ: Mapping[str, str]
    stdout: TextIO
    stderr: TextIO
    as_json: bool = False
    _telemetry_recorded: bool = field(default=False, repr=False)

    def record_outcome(self, subcommand: str, outcome: str) -> None:
        """Record `worktrack_cli_commands_total` at most once per CLI invocation."""
        if self._telemetry_recorded:
            return
        bounded = normalize_cli_subcommand(subcommand)
        self.telemetry.record_cli_command(subcommand=bounded, outcome=outcome)
        self._telemetry_recorded = True

    @contextmanager
    def open_ledger(self) -> Iterator[Ledger]:
        """Open a migrated SQLite `Ledger` bound to `settings.db_path`."""
        with Ledger.from_settings(
            self.settings,
            telemetry=self.telemetry,
            environ=self.environ,
        ) as ledger:
            yield ledger

    @contextmanager
    def open_planner(self, *, ledger: Ledger | None = None) -> Iterator[AgendaPlanner]:
        """Open a migrated `AgendaPlanner` bound to `settings.db_path`."""
        with AgendaPlanner.from_settings(
            self.settings,
            ledger=ledger,
            telemetry=self.telemetry,
            environ=self.environ,
        ) as planner:
            yield planner

    @contextmanager
    def open_outbox_store(self) -> Iterator[OutboxStore]:
        """Open a migrated `OutboxStore` bound to `settings.db_path`."""
        with OutboxStore.from_settings(
            self.settings,
            telemetry=self.telemetry,
            environ=self.environ,
        ) as store:
            yield store


# endregion


# region Resolution
def normalize_cli_subcommand(raw: str | None) -> str:
    """Map an arbitrary CLI token onto the bounded canonical subcommand allowlist."""
    if raw is None:
        return SUBCOMMAND_HELP
    cleaned = raw.strip().lower()
    if cleaned in ("-h", "--help"):
        return SUBCOMMAND_HELP
    if cleaned in CANONICAL_CLI_SUBCOMMANDS:
        return cleaned
    return SUBCOMMAND_UNKNOWN


def detect_subcommand_from_argv(argv: Sequence[str]) -> str:
    """Extract the first positional subcommand token from raw `argv`."""
    for token in argv:
        if token in ("-h", "--help"):
            return SUBCOMMAND_HELP
        if not token.startswith("-"):
            return normalize_cli_subcommand(token)
    return SUBCOMMAND_HELP


def resolve_or_create_activity(
    ledger: Ledger,
    identifier: str,
    *,
    kind: str = ACTIVITY_KIND_TARGETED,
    external_ref: str | None = None,
    title: str | None = None,
    tags: Sequence[str] = (),
    create_if_missing: bool = True,
) -> Activity:
    """Resolve an `Activity` by ID, `external_ref`, or exact name, or create on the fly."""
    clean_ident = identifier.strip()
    if not clean_ident:
        raise LedgerValidationError("Activity identifier or name must be non-empty.")

    found = _find_existing_activity(ledger, clean_ident, external_ref=external_ref)
    if found is not None:
        if external_ref is not None and found.external_ref is None:
            return ledger.update_activity(found.id, external_ref=external_ref)
        return found

    if not create_if_missing:
        raise ActivityNotFoundError(f"Activity '{clean_ident}' does not exist.")

    resolved_name, resolved_ref = _resolve_new_activity_fields(
        clean_ident, external_ref=external_ref, title=title
    )
    return ledger.create_activity(
        resolved_name,
        kind=kind,
        external_ref=resolved_ref,
        tags=tags,
    )


def _resolve_new_activity_fields(
    clean_ident: str,
    *,
    external_ref: str | None,
    title: str | None,
) -> tuple[str, str | None]:
    """Derive `(name, external_ref)` when creating a missing `Activity` on the fly."""
    clean_title = title.strip() if title else ""
    resolved_name = clean_title or clean_ident
    if external_ref is not None:
        return resolved_name, external_ref
    if clean_title and clean_title != clean_ident:
        return resolved_name, clean_ident
    return resolved_name, None


def _find_existing_activity(
    ledger: Ledger,
    identifier: str,
    *,
    external_ref: str | None = None,
) -> Activity | None:
    """Look up an existing `Activity` by ID, `external_ref`, or exact `name`."""
    try:
        return ledger.get_activity(identifier)
    except ActivityNotFoundError:
        return _find_activity_by_ref_or_name(ledger, identifier, external_ref=external_ref)


def _find_activity_by_ref_or_name(
    ledger: Ledger,
    identifier: str,
    *,
    external_ref: str | None = None,
) -> Activity | None:
    """Look up an `Activity` by explicit/implicit `external_ref` or exact `name`."""
    if external_ref is not None:
        by_explicit_ref = ledger.find_activity_by_external_ref(external_ref)
        if by_explicit_ref is not None:
            return by_explicit_ref

    by_ident_ref = ledger.find_activity_by_external_ref(identifier)
    if by_ident_ref is not None:
        return by_ident_ref

    for candidate in ledger.list_activities(include_archived=True):
        if candidate.name == identifier:
            return candidate
    return None


def parse_cli_timestamp(raw: str, *, field_name: str = "timestamp") -> datetime:
    """Parse an ISO-8601 timestamp (or `HH:MM` UTC shorthand) into a UTC `datetime`."""
    stripped = raw.strip()
    if not stripped:
        raise LedgerValidationError(f"{field_name} must be a non-empty timestamp.")
    if len(stripped) == HH_MM_COLON_LEN and ":" in stripped:
        today = datetime.now(UTC).date()
        stripped = f"{today.isoformat()}T{stripped}:00Z"
    try:
        parsed = datetime.fromisoformat(stripped)
    except ValueError as exc:
        raise LedgerValidationError(f"Invalid ISO-8601 {field_name}: '{raw}'.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def coerce_cli_facet_value(facet: Facet, raw_value: str) -> FacetValue:
    """Coerce a CLI `<key>=<value>` string into the target `Facet`'s Python type."""
    if facet.value_type in (FACET_TYPE_ENUM, FACET_TYPE_STRING):
        return raw_value
    if facet.value_type == FACET_TYPE_INT:
        try:
            return int(raw_value.strip())
        except ValueError as exc:
            raise LedgerValidationError(
                f"Facet '{facet.key}' expects an integer, got '{raw_value}'."
            ) from exc
    if facet.value_type == FACET_TYPE_FLOAT:
        try:
            return float(raw_value.strip())
        except ValueError as exc:
            raise LedgerValidationError(
                f"Facet '{facet.key}' expects a number, got '{raw_value}'."
            ) from exc
    if facet.value_type == FACET_TYPE_BOOL:
        lowered = raw_value.strip().lower()
        if lowered in BOOL_TRUE_LITERALS:
            return True
        if lowered in BOOL_FALSE_LITERALS:
            return False
        raise LedgerValidationError(f"Facet '{facet.key}' expects a boolean (true/false).")
    return raw_value


def serialize_context_fact(fact: ContextFact) -> dict[str, object]:
    """Serialize a `ContextFact` into a JSON-compatible dictionary."""
    _ = UNSET
    return {
        "id": fact.id,
        "facet_key": fact.facet_key,
        "value": fact.value,
        "scope_date": fact.scope_date,
        "interval_id": fact.interval_id,
        "source": fact.source,
        "updated_at": format_utc_iso(fact.updated_at),
    }


# endregion
