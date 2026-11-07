"""`GitClueCollector` scanning local Git repositories and remote forges into `ClueStore`."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from worktrack.collectors.forge import ForgeCommitSource
from worktrack.collectors.git_runner import GitLogRunner, SubprocessGitLogRunner
from worktrack.collectors.models import (
    CLUE_SOURCE_FORGE,
    CLUE_SOURCE_GIT,
    DEFAULT_FORGE_CONFIDENCE,
    DEFAULT_GIT_COMMIT_CONFIDENCE,
    DEFAULT_GIT_MAX_COMMITS,
    DEFAULT_ISSUE_KEY_PATTERN,
    ENV_GIT_AUTHOR_PATTERN,
    ENV_ISSUE_KEY_PATTERN,
    METRIC_SOURCE_SYNCS_TOTAL,
    OUTCOME_ERROR,
    OUTCOME_OK,
    Clue,
    ClueValidationError,
    CollectorSyncSummary,
    GitCommitRecord,
    compile_issue_key_regex,
    extract_issue_keys,
    validate_clue_confidence,
    validate_clue_timestamp,
)
from worktrack.collectors.store import ClueStore
from worktrack.core import Activity, Ledger
from worktrack.telemetry.metrics import TelemetryRecorder


# region Git Collector
class GitClueCollector:
    """Collector scanning local `git log` history and forge APIs into deduplicated `Clues`."""

    def __init__(
        self,
        *,
        clue_store: ClueStore,
        ledger: Ledger | None = None,
        repo_paths: Sequence[Path | str] = (),
        git_runner: GitLogRunner | None = None,
        forge_sources: Sequence[ForgeCommitSource] = (),
        author_pattern: str | None = None,
        issue_key_pattern: str | re.Pattern[str] | None = None,
        commit_confidence: float = DEFAULT_GIT_COMMIT_CONFIDENCE,
        forge_confidence: float = DEFAULT_FORGE_CONFIDENCE,
        max_commits: int = DEFAULT_GIT_MAX_COMMITS,
        telemetry: TelemetryRecorder | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize `GitClueCollector` with repositories, forge sources, and regex."""
        self._clue_store = clue_store
        self._ledger = ledger
        self._repo_paths = tuple(Path(p) for p in repo_paths)
        self._git_runner: GitLogRunner = (
            SubprocessGitLogRunner() if git_runner is None else git_runner
        )
        self._forge_sources = tuple(forge_sources)
        self._default_author = _resolve_author_pattern(author_pattern, environ)
        self._issue_key_regex = _resolve_issue_key_regex(issue_key_pattern, environ)
        self._commit_confidence = validate_clue_confidence(commit_confidence)
        self._forge_confidence = validate_clue_confidence(forge_confidence)
        self._max_commits = max(1, max_commits)
        self._telemetry = telemetry

    def sync(
        self,
        *,
        repo_paths: Sequence[Path | str] | None = None,
        forge_sources: Sequence[ForgeCommitSource] | None = None,
        author_pattern: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        ledger: Ledger | None = None,
    ) -> CollectorSyncSummary:
        """Scan configured Git repositories and forge sources and upsert local `Clues`."""
        try:
            utc_since, utc_until = _validate_window(since, until)
            active_repos = (
                self._repo_paths if repo_paths is None else tuple(Path(p) for p in repo_paths)
            )
            active_forges = self._forge_sources if forge_sources is None else tuple(forge_sources)
            resolved_author = (
                self._default_author if author_pattern is None else author_pattern.strip() or None
            )
            active_ledger = self._ledger if ledger is None else ledger

            git_records = self._collect_local_git_records(
                active_repos,
                author_pattern=resolved_author,
                since=utc_since,
                until=utc_until,
            )
            forge_records = self._collect_forge_records(
                active_forges,
                author_pattern=resolved_author,
                since=utc_since,
                until=utc_until,
            )
            upserted_clues, linked_count = self._persist_collected_records(
                git_records=git_records,
                forge_records=forge_records,
                ledger=active_ledger,
            )
        except Exception:
            self._record_sync_metric(OUTCOME_ERROR)
            raise

        self._record_sync_metric(OUTCOME_OK)
        return CollectorSyncSummary(
            commits_scanned=len(git_records),
            forge_records_scanned=len(forge_records),
            clues_upserted=len(upserted_clues),
            activities_linked=linked_count,
            clues=tuple(upserted_clues),
        )

    def _collect_local_git_records(
        self,
        repos: Sequence[Path],
        *,
        author_pattern: str | None,
        since: datetime | None,
        until: datetime | None,
    ) -> list[GitCommitRecord]:
        """Collect `GitCommitRecord` items across all configured local repositories."""
        collected: list[GitCommitRecord] = []
        for repo_path in repos:
            records = self._git_runner.list_commits(
                repo_path,
                author_pattern=author_pattern,
                since=since,
                until=until,
                max_commits=self._max_commits,
            )
            collected.extend(records)
        return collected

    def _collect_forge_records(
        self,
        sources: Sequence[ForgeCommitSource],
        *,
        author_pattern: str | None,
        since: datetime | None,
        until: datetime | None,
    ) -> list[GitCommitRecord]:
        """Collect `GitCommitRecord` items across all configured `ForgeCommitSource`s."""
        collected: list[GitCommitRecord] = []
        for source in sources:
            records = source.fetch_records(
                author_pattern=author_pattern,
                since=since,
                until=until,
            )
            collected.extend(records)
        return collected

    def _persist_collected_records(
        self,
        *,
        git_records: Sequence[GitCommitRecord],
        forge_records: Sequence[GitCommitRecord],
        ledger: Ledger | None,
    ) -> tuple[list[Clue], int]:
        """Extract issue keys, resolve `Activity` links, and upsert `Clues`."""
        activity_lookup = _build_activity_lookup(ledger)
        persisted: list[Clue] = []
        linked_count = 0

        for record in git_records:
            clue, is_linked = self._upsert_record_clue(
                record,
                clue_source=CLUE_SOURCE_GIT,
                confidence=self._commit_confidence,
                activity_lookup=activity_lookup,
            )
            persisted.append(clue)
            if is_linked:
                linked_count += 1

        for record in forge_records:
            clue, is_linked = self._upsert_record_clue(
                record,
                clue_source=CLUE_SOURCE_FORGE,
                confidence=self._forge_confidence,
                activity_lookup=activity_lookup,
            )
            persisted.append(clue)
            if is_linked:
                linked_count += 1

        return persisted, linked_count

    def _upsert_record_clue(
        self,
        record: GitCommitRecord,
        *,
        clue_source: str,
        confidence: float,
        activity_lookup: Mapping[str, str],
    ) -> tuple[Clue, bool]:
        """Convert a single `GitCommitRecord` into an idempotent `Clue` row."""
        matched_keys = extract_issue_keys(
            (record.subject, record.body, record.branch),
            self._issue_key_regex,
        )
        primary_key = matched_keys[0] if matched_keys else None
        linked_activity_id = _resolve_linked_activity_id(matched_keys, activity_lookup)
        external_key = f"{record.repository}:{record.record_kind}:{record.commit_sha}"
        metadata: dict[str, object] = {
            "repository": record.repository,
            "commit_sha": record.commit_sha,
            "record_kind": record.record_kind,
            "author_email": record.author_email,
            "author_name": record.author_name,
            "branch": record.branch,
            "matched_keys": list(matched_keys),
            "url": record.url,
        }
        clue = self._clue_store.upsert_clue(
            source=clue_source,
            external_key=external_key,
            occurred_at=record.occurred_at,
            summary=record.subject,
            inferred_activity_key=primary_key,
            activity_id=linked_activity_id,
            confidence=confidence,
            metadata=metadata,
        )
        return clue, linked_activity_id is not None

    def _record_sync_metric(self, outcome: str) -> None:
        """Increment `worktrack_source_syncs_total{source='git', outcome=...}`."""
        if self._telemetry is not None:
            self._telemetry.increment_counter(
                METRIC_SOURCE_SYNCS_TOTAL,
                {"source": CLUE_SOURCE_GIT, "outcome": outcome},
            )


def _resolve_author_pattern(
    explicit_author: str | None,
    environ: Mapping[str, str] | None,
) -> str | None:
    """Resolve optional git author filter from argument or `WORKTRACK_GIT_AUTHOR_PATTERN`."""
    if explicit_author is not None and explicit_author.strip():
        return explicit_author.strip()
    env = os.environ if environ is None else environ
    env_author = env.get(ENV_GIT_AUTHOR_PATTERN, "").strip()
    return env_author or None


def _resolve_issue_key_regex(
    explicit_pattern: str | re.Pattern[str] | None,
    environ: Mapping[str, str] | None,
) -> re.Pattern[str]:
    """Resolve and compile issue-key regex from argument, env var, or default."""
    if explicit_pattern is not None:
        return compile_issue_key_regex(explicit_pattern)
    env = os.environ if environ is None else environ
    env_pattern = env.get(ENV_ISSUE_KEY_PATTERN, "").strip()
    return compile_issue_key_regex(env_pattern or DEFAULT_ISSUE_KEY_PATTERN)


def _validate_window(
    since: datetime | None,
    until: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """Validate optional `[since, until)` time window."""
    utc_since = None if since is None else validate_clue_timestamp(since, field_name="since")
    utc_until = None if until is None else validate_clue_timestamp(until, field_name="until")
    if utc_since is not None and utc_until is not None and utc_until <= utc_since:
        raise ClueValidationError("until must be strictly greater than since.")
    return utc_since, utc_until


def _build_activity_lookup(ledger: Ledger | None) -> dict[str, str]:
    """Build lookup map from `external_ref` / `rollup_target_ref` to `Activity.id`."""
    if ledger is None:
        return {}
    lookup: dict[str, str] = {}
    activities: list[Activity] = ledger.list_activities(include_archived=True)
    for act in activities:
        lookup[act.id] = act.id
        if act.external_ref:
            lookup[act.external_ref] = act.id
        if act.rollup_target_ref:
            lookup.setdefault(act.rollup_target_ref, act.id)
    return lookup


def _resolve_linked_activity_id(
    matched_keys: Sequence[str],
    activity_lookup: Mapping[str, str],
) -> str | None:
    """Return the first `Activity.id` matching any extracted issue key."""
    for key in matched_keys:
        act_id = activity_lookup.get(key)
        if act_id is not None:
            return act_id
    return None


# endregion
