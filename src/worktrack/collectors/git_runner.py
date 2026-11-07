"""Local `GitLogRunner` protocol and `git log` subprocess implementation."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from worktrack.collectors.models import (
    DEFAULT_GIT_MAX_COMMITS,
    GIT_RECORD_KIND_COMMIT,
    GitCollectorError,
    GitCommitRecord,
    validate_clue_timestamp,
)

# region Constants
GIT_EXECUTABLE: Final = "git"
GIT_FIELD_SEPARATOR: Final = "\x1f"
GIT_RECORD_SEPARATOR: Final = "\x1e"
GIT_LOG_PRETTY_FORMAT: Final = "%H%x1f%aI%x1f%an%x1f%ae%x1f%D%x1f%s%x1f%b%x1e"
EXPECTED_GIT_LOG_FIELDS: Final = 7
DEFAULT_GIT_SUBPROCESS_TIMEOUT_SECONDS: Final = 15.0
EMPTY_REPO_STDERR_MARKER: Final = "does not have any commits yet"
# endregion


# region Protocols
class GitLogRunner(Protocol):
    """Injectable protocol scanning local Git repository commit history."""

    def list_commits(
        self,
        repo_path: Path,
        *,
        author_pattern: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        max_commits: int = DEFAULT_GIT_MAX_COMMITS,
    ) -> Sequence[GitCommitRecord]:
        """Return commits from `repo_path` matching optional author and `[since, until)`."""
        ...


# endregion


# region Git Log Runner
class SubprocessGitLogRunner:
    """Default `GitLogRunner` invoking local `git log` in a bounded subprocess."""

    def __init__(
        self,
        *,
        git_executable: str = GIT_EXECUTABLE,
        timeout_seconds: float = DEFAULT_GIT_SUBPROCESS_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize subprocess runner with git executable name and timeout."""
        self._git_executable = git_executable
        self._timeout_seconds = timeout_seconds

    def list_commits(
        self,
        repo_path: Path,
        *,
        author_pattern: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        max_commits: int = DEFAULT_GIT_MAX_COMMITS,
    ) -> list[GitCommitRecord]:
        """Run `git log` in `repo_path` and filter commits by author and `[since, until)`."""
        resolved_repo = Path(repo_path)
        if not resolved_repo.is_dir():
            raise GitCollectorError(
                f"Git repository directory '{resolved_repo.name}' does not exist."
            )
        utc_since = None if since is None else validate_clue_timestamp(since, field_name="since")
        utc_until = None if until is None else validate_clue_timestamp(until, field_name="until")
        cmd = _build_git_log_command(
            self._git_executable,
            resolved_repo,
            author_pattern=author_pattern,
            max_commits=max(1, max_commits),
        )
        raw_output = _run_git_subprocess(cmd, resolved_repo.name, self._timeout_seconds)
        repo_label = resolved_repo.name or "repository"
        parsed = _parse_git_log_output(raw_output, repo_label=repo_label)
        return [
            record
            for record in parsed
            if _matches_commit_filters(
                record,
                author_pattern=author_pattern,
                since=utc_since,
                until=utc_until,
            )
        ]


def _build_git_log_command(
    git_executable: str,
    repo_path: Path,
    *,
    author_pattern: str | None,
    max_commits: int,
) -> list[str]:
    """Assemble deterministic `git log` command arguments."""
    cmd = [
        git_executable,
        "-C",
        str(repo_path),
        "log",
        "--all",
        f"--max-count={max_commits}",
        f"--pretty=format:{GIT_LOG_PRETTY_FORMAT}",
    ]
    if author_pattern is not None and author_pattern.strip():
        cmd.append(f"--author={author_pattern.strip()}")
    return cmd


def _run_git_subprocess(
    cmd: Sequence[str],
    repo_name: str,
    timeout_seconds: float,
) -> str:
    """Execute `git log` subprocess and return stdout or raise `GitCollectorError`."""
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitCollectorError(f"Failed to execute git log in '{repo_name}': {exc}") from exc

    if completed.returncode != 0:
        stderr_text = completed.stderr.strip()
        if EMPTY_REPO_STDERR_MARKER in stderr_text:
            return ""
        raise GitCollectorError(f"git log failed in '{repo_name}': {stderr_text}")
    return completed.stdout


def _parse_git_log_output(raw_output: str, *, repo_label: str) -> list[GitCommitRecord]:
    r"""Parse `\x1e`/`\x1f`-delimited `git log` output into `GitCommitRecord` items."""
    records: list[GitCommitRecord] = []
    for raw_entry in raw_output.split(GIT_RECORD_SEPARATOR):
        cleaned = raw_entry.strip()
        if not cleaned:
            continue
        parts = cleaned.split(GIT_FIELD_SEPARATOR)
        if len(parts) < EXPECTED_GIT_LOG_FIELDS:
            continue
        sha, iso_date, author_name, author_email, ref_names, subject, body = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip(),
            parts[3].strip(),
            parts[4].strip(),
            parts[5].strip(),
            parts[6].strip(),
        )
        if not sha or not iso_date or not subject:
            continue
        occurred_at = datetime.fromisoformat(iso_date).astimezone(UTC)
        records.append(
            GitCommitRecord(
                commit_sha=sha,
                occurred_at=occurred_at,
                subject=subject,
                author_email=author_email,
                author_name=author_name,
                repository=repo_label,
                branch=ref_names or None,
                body=body,
                record_kind=GIT_RECORD_KIND_COMMIT,
            )
        )
    return records


def _matches_commit_filters(
    record: GitCommitRecord,
    *,
    author_pattern: str | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    """Verify `record` satisfies author substring/pattern and `[since, until)` bounds."""
    if author_pattern is not None and author_pattern.strip():
        needle = author_pattern.strip().lower()
        haystack = f"{record.author_name} <{record.author_email}>".lower()
        if needle not in haystack:
            return False
    if since is not None and record.occurred_at < since:
        return False
    return not (until is not None and record.occurred_at >= until)


# endregion
