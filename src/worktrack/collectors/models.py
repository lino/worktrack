"""Domain models, constants, exceptions, and validators for `worktrack.collectors`."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from worktrack.core.exceptions import LedgerError
from worktrack.core.models import (
    MAX_IDENTIFIER_LENGTH,
    ensure_utc,
    format_utc_iso,
)

# region Constants
CLUE_SCHEMA_VERSION_INITIAL: Final = 1
EMPTY_CLUE_SCHEMA_VERSION: Final = 0

CLUE_SOURCE_GIT: Final = "git"
CLUE_SOURCE_FORGE: Final = "forge"
CLUE_SOURCE_SENSOR: Final = "sensor"
CLUE_SOURCE_CALENDAR: Final = "calendar"
CLUE_SOURCE_MANUAL: Final = "manual"

GIT_RECORD_KIND_COMMIT: Final = "commit"
GIT_RECORD_KIND_PULL_REQUEST: Final = "pull_request"
GIT_RECORD_KIND_MERGE_REQUEST: Final = "merge_request"

DEFAULT_CLUE_CONFIDENCE: Final = 0.80
DEFAULT_GIT_COMMIT_CONFIDENCE: Final = 0.85
DEFAULT_FORGE_CONFIDENCE: Final = 0.90
MIN_CLUE_CONFIDENCE: Final = 0.0
MAX_CLUE_CONFIDENCE: Final = 1.0

DEFAULT_ISSUE_KEY_PATTERN: Final = r"\b([A-Z][A-Z0-9_]{1,15}-\d+)\b"
ENV_ISSUE_KEY_PATTERN: Final = "WORKTRACK_ISSUE_KEY_PATTERN"
ENV_GIT_AUTHOR_PATTERN: Final = "WORKTRACK_GIT_AUTHOR_PATTERN"

DEFAULT_GIT_MAX_COMMITS: Final = 500
DEFAULT_REPOSITORY_LABEL: Final = "local"
MAX_SUMMARY_LENGTH: Final = 512
MAX_EXTERNAL_KEY_LENGTH: Final = 256

METRIC_SOURCE_SYNCS_TOTAL: Final = "worktrack_source_syncs_total"
OUTCOME_OK: Final = "ok"
OUTCOME_ERROR: Final = "error"
# endregion


# region Exceptions
class CollectorError(LedgerError):
    """Base exception for ambient `Clue` collectors and `ClueStore` operations."""


class ClueMigrationError(CollectorError):
    """Raised when `clue_schema_migrations` fails or detects a checksum mismatch."""


class ClueValidationError(CollectorError):
    """Raised when a `Clue` or collector configuration fails validation."""


class ClueNotFoundError(CollectorError):
    """Raised when a requested `Clue` ID does not exist in SQLite."""


class GitCollectorError(CollectorError):
    """Raised when scanning a local Git repository fails."""


class ForgeTransportError(CollectorError):
    """Raised when communicating with a GitHub or GitLab forge endpoint fails."""


class ForgeAuthenticationError(CollectorError):
    """Raised when a forge OAuth token is missing or rejected."""


# endregion


# region Models
@dataclass(frozen=True, slots=True)
class Clue:
    """Ambient local work hint captured by a `Collector` or `Sensor`.

    Attributes:
        id: Primary UUID identifier of the `Clue`.
        source: Collector/sensor source slug (e.g. `git`, `forge`, `sensor`).
        external_key: Source-scoped deterministic idempotency key.
        occurred_at: UTC timestamp when the hinted event occurred.
        summary: Human-readable summary of the commit, MR, or sensor match.
        inferred_activity_key: Optional extracted issue/Activity key (e.g. `PROJ-123`).
        confidence: Plausibility confidence score in `[0.0, 1.0]`.
        metadata: Structured provenance dictionary stored locally in SQLite.
        created_at: UTC timestamp when the `Clue` row was first created.
        activity_id: Optional linked `Activity.id` in the local `Ledger`.
    """

    id: str
    source: str
    external_key: str
    occurred_at: datetime
    summary: str
    inferred_activity_key: str | None = None
    confidence: float = DEFAULT_CLUE_CONFIDENCE
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    activity_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize `Clue` to a JSON-compatible dictionary."""
        return {
            "id": self.id,
            "source": self.source,
            "external_key": self.external_key,
            "occurred_at": format_utc_iso(self.occurred_at),
            "summary": self.summary,
            "inferred_activity_key": self.inferred_activity_key,
            "activity_id": self.activity_id,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
            "created_at": format_utc_iso(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class GitCommitRecord:
    """Normalized Git commit or forge merge/pull request event record."""

    commit_sha: str
    occurred_at: datetime
    subject: str
    author_email: str = ""
    author_name: str = ""
    repository: str = DEFAULT_REPOSITORY_LABEL
    branch: str | None = None
    body: str = ""
    record_kind: str = GIT_RECORD_KIND_COMMIT
    url: str | None = None

    def __post_init__(self) -> None:
        """Normalize `occurred_at` to timezone-aware UTC."""
        try:
            utc_ts = ensure_utc(self.occurred_at, field_name="occurred_at")
        except LedgerError as exc:
            raise ClueValidationError(str(exc)) from exc
        object.__setattr__(self, "occurred_at", utc_ts)

    @property
    def sha(self) -> str:
        """Return canonical commit SHA identifier alias."""
        return self.commit_sha

    @property
    def message(self) -> str:
        """Return full commit message combining `subject` and `body`."""
        if not self.body.strip():
            return self.subject.strip()
        return f"{self.subject.strip()}\n\n{self.body.strip()}"


@dataclass(frozen=True, slots=True)
class CollectorSyncSummary:
    """Summary counts and persisted `Clue` records from a `GitClueCollector.sync` run."""

    commits_scanned: int
    forge_records_scanned: int
    clues_upserted: int
    activities_linked: int
    clues: tuple[Clue, ...] = ()


# endregion


# region Validation
def validate_clue_confidence(confidence: float) -> float:
    """Validate that `confidence` is a finite float in `[0.0, 1.0]`."""
    if isinstance(confidence, bool) or not math.isfinite(confidence):
        raise ClueValidationError("Clue confidence must be a finite float.")
    numeric = float(confidence)
    if not (MIN_CLUE_CONFIDENCE <= numeric <= MAX_CLUE_CONFIDENCE):
        raise ClueValidationError(
            f"Clue confidence must satisfy 0.0 <= confidence <= 1.0, got {numeric}."
        )
    return numeric


def validate_clue_text(
    value: str,
    *,
    field_name: str,
    max_len: int = MAX_SUMMARY_LENGTH,
) -> str:
    """Validate and strip a required non-empty text field for a `Clue`."""
    if not isinstance(value, str):
        raise ClueValidationError(f"{field_name} must be a string.")
    stripped = value.strip()
    if not stripped or len(stripped) > max_len:
        raise ClueValidationError(f"{field_name} must be 1..{max_len} non-blank characters.")
    return stripped


def validate_optional_clue_text(
    value: str | None,
    *,
    field_name: str,
    max_len: int = MAX_IDENTIFIER_LENGTH,
) -> str | None:
    """Validate and strip an optional identifier field on a `Clue`."""
    if value is None:
        return None
    return validate_clue_text(value, field_name=field_name, max_len=max_len)


def validate_clue_timestamp(timestamp: datetime, *, field_name: str = "occurred_at") -> datetime:
    """Ensure `timestamp` is timezone-aware and normalized to UTC."""
    try:
        return ensure_utc(timestamp, field_name=field_name)
    except LedgerError as exc:
        raise ClueValidationError(str(exc)) from exc


def compile_issue_key_regex(pattern: str | re.Pattern[str]) -> re.Pattern[str]:
    """Compile and validate a regular expression for extracting issue/Activity keys."""
    if isinstance(pattern, re.Pattern):
        return pattern
    cleaned = pattern.strip()
    if not cleaned:
        raise ClueValidationError("issue_key_pattern must be a non-empty regular expression.")
    try:
        return re.compile(cleaned)
    except re.error as exc:
        raise ClueValidationError(f"Invalid issue_key_pattern '{pattern}': {exc}") from exc


def extract_issue_keys(
    texts: Sequence[str | None],
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    """Extract ordered, deduplicated issue/Activity keys from candidate text fragments."""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in texts:
        if not raw:
            continue
        for match in pattern.finditer(raw):
            token = (match.group(1) if match.lastindex else match.group(0)).strip()
            if token and token not in seen:
                seen.add(token)
                ordered.append(token)
    return tuple(ordered)


# endregion
