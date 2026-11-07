"""GitHub and GitLab REST JSON payload parsers for `worktrack.collectors`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from worktrack.collectors.models import (
    GIT_RECORD_KIND_COMMIT,
    GIT_RECORD_KIND_MERGE_REQUEST,
    GIT_RECORD_KIND_PULL_REQUEST,
    GitCommitRecord,
)


def parse_github_commits_payload(
    items: Sequence[Mapping[str, object]],
    *,
    repository: str,
) -> list[GitCommitRecord]:
    """Parse GitHub `/repos/{owner}/{repo}/commits` JSON array into `GitCommitRecord`s."""
    records: list[GitCommitRecord] = []
    for item in items:
        parsed = _parse_github_commit_item(item, repository=repository)
        if parsed is not None:
            records.append(parsed)
    return records


def _parse_github_commit_item(
    item: Mapping[str, object],
    *,
    repository: str,
) -> GitCommitRecord | None:
    """Parse a single GitHub commit JSON object into a `GitCommitRecord`."""
    sha = str(item.get("sha", "")).strip()
    commit_map = _as_mapping(item.get("commit"))
    author_map = _as_mapping(commit_map.get("author"))
    raw_date = str(author_map.get("date", "")).strip()
    raw_msg = str(commit_map.get("message", "")).strip()
    if not sha or not raw_date or not raw_msg:
        return None
    subject, body = _split_subject_and_body(raw_msg)
    return GitCommitRecord(
        commit_sha=sha,
        occurred_at=_parse_iso_datetime(raw_date),
        subject=subject,
        author_email=str(author_map.get("email", "")).strip(),
        author_name=str(author_map.get("name", "")).strip(),
        repository=repository,
        body=body,
        record_kind=GIT_RECORD_KIND_COMMIT,
        url=_optional_str(item.get("html_url")),
    )


def parse_github_pulls_payload(
    items: Sequence[Mapping[str, object]],
    *,
    repository: str,
) -> list[GitCommitRecord]:
    """Parse GitHub `/repos/{owner}/{repo}/pulls` JSON array into `GitCommitRecord`s."""
    records: list[GitCommitRecord] = []
    for item in items:
        parsed = _parse_github_pull_item(item, repository=repository)
        if parsed is not None:
            records.append(parsed)
    return records


def _parse_github_pull_item(
    item: Mapping[str, object],
    *,
    repository: str,
) -> GitCommitRecord | None:
    """Parse a single GitHub pull request JSON object into a `GitCommitRecord`."""
    number = item.get("number")
    title = str(item.get("title", "")).strip()
    raw_date = _first_non_empty_str(item.get("merged_at"), item.get("updated_at"))
    if number is None or not title or not raw_date:
        return None
    head_map = _as_mapping(item.get("head"))
    user_map = _as_mapping(item.get("user"))
    sha = _first_non_empty_str(head_map.get("sha"), f"pr-{number}")
    return GitCommitRecord(
        commit_sha=f"pr-{number}-{sha}",
        occurred_at=_parse_iso_datetime(raw_date),
        subject=title,
        author_name=str(user_map.get("login", "")).strip(),
        repository=repository,
        branch=_optional_str(head_map.get("ref")),
        body=_first_non_empty_str(item.get("body")),
        record_kind=GIT_RECORD_KIND_PULL_REQUEST,
        url=_optional_str(item.get("html_url")),
    )


def parse_gitlab_commits_payload(
    items: Sequence[Mapping[str, object]],
    *,
    repository: str,
) -> list[GitCommitRecord]:
    """Parse GitLab `/projects/:id/repository/commits` JSON array into `GitCommitRecord`s."""
    records: list[GitCommitRecord] = []
    for item in items:
        parsed = _parse_gitlab_commit_item(item, repository=repository)
        if parsed is not None:
            records.append(parsed)
    return records


def _parse_gitlab_commit_item(
    item: Mapping[str, object],
    *,
    repository: str,
) -> GitCommitRecord | None:
    """Parse a single GitLab commit JSON object into a `GitCommitRecord`."""
    sha = str(item.get("id", "")).strip()
    raw_date = _first_non_empty_str(item.get("committed_date"), item.get("created_at"))
    raw_msg = _first_non_empty_str(item.get("message"), item.get("title"))
    if not sha or not raw_date or not raw_msg:
        return None
    subject, body = _split_subject_and_body(raw_msg)
    return GitCommitRecord(
        commit_sha=sha,
        occurred_at=_parse_iso_datetime(raw_date),
        subject=subject,
        author_email=str(item.get("author_email", "")).strip(),
        author_name=str(item.get("author_name", "")).strip(),
        repository=repository,
        body=body,
        record_kind=GIT_RECORD_KIND_COMMIT,
        url=_optional_str(item.get("web_url")),
    )


def parse_gitlab_merge_requests_payload(
    items: Sequence[Mapping[str, object]],
    *,
    repository: str,
) -> list[GitCommitRecord]:
    """Parse GitLab `/projects/:id/merge_requests` JSON array into `GitCommitRecord`s."""
    records: list[GitCommitRecord] = []
    for item in items:
        parsed = _parse_gitlab_mr_item(item, repository=repository)
        if parsed is not None:
            records.append(parsed)
    return records


def _parse_gitlab_mr_item(
    item: Mapping[str, object],
    *,
    repository: str,
) -> GitCommitRecord | None:
    """Parse a single GitLab merge request JSON object into a `GitCommitRecord`."""
    iid = item.get("iid")
    title = str(item.get("title", "")).strip()
    raw_date = _first_non_empty_str(item.get("merged_at"), item.get("updated_at"))
    if iid is None or not title or not raw_date:
        return None
    author_map = _as_mapping(item.get("author"))
    sha = _first_non_empty_str(item.get("sha"), f"mr-{iid}")
    return GitCommitRecord(
        commit_sha=f"mr-{iid}-{sha}",
        occurred_at=_parse_iso_datetime(raw_date),
        subject=title,
        author_name=str(author_map.get("username", "")).strip(),
        repository=repository,
        branch=_optional_str(item.get("source_branch")),
        body=_first_non_empty_str(item.get("description")),
        record_kind=GIT_RECORD_KIND_MERGE_REQUEST,
        url=_optional_str(item.get("web_url")),
    )


def matches_forge_filters(
    record: GitCommitRecord,
    *,
    author_pattern: str | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    """Check whether a forge `GitCommitRecord` matches optional author and time window."""
    if author_pattern is not None and author_pattern.strip():
        needle = author_pattern.strip().lower()
        haystack = f"{record.author_name} {record.author_email}".lower()
        if needle not in haystack:
            return False
    if since is not None and record.occurred_at < since:
        return False
    return not (until is not None and record.occurred_at >= until)


def _as_mapping(value: object) -> Mapping[str, object]:
    """Return `value` if it is a `Mapping`, otherwise an empty dict."""
    if isinstance(value, Mapping):
        return value
    return {}


def _first_non_empty_str(*candidates: object) -> str:
    """Return the first candidate that converts to a non-empty stripped string."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _split_subject_and_body(message: str) -> tuple[str, str]:
    """Split a commit message into its first-line subject and remaining body."""
    lines = message.strip().splitlines()
    if not lines:
        return "", ""
    subject = lines[0].strip()
    body = "\n".join(lines[1:]).strip()
    return subject, body


def _parse_iso_datetime(raw_iso: str) -> datetime:
    """Parse an ISO-8601 timestamp string (supporting trailing `Z`) into UTC."""
    normalized = raw_iso.replace("Z", "+00:00") if raw_iso.endswith("Z") else raw_iso
    return datetime.fromisoformat(normalized).astimezone(UTC)


def _optional_str(value: object) -> str | None:
    """Return stripped non-empty string or `None`."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
