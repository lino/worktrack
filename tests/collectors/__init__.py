"""Behavioural test package and fixtures for `worktrack.collectors`."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from worktrack.collectors import ForgeHttpResponse, GitCommitRecord

BASE_TIMESTAMP = datetime(2026, 11, 7, 9, 0, 0, tzinfo=UTC)
HTTP_STATUS_OK = 200
SAMPLE_STATIC_COMMITS: tuple[GitCommitRecord, ...] = (
    GitCommitRecord(
        commit_sha="1111111111111111111111111111111111111111",
        occurred_at=BASE_TIMESTAMP + timedelta(minutes=10),
        author_name="Alice",
        author_email="alice@example.org",
        subject="feat: implement collector store",
        branch="HEAD -> feature/WT-42-clues",
        repository="worktrack",
    ),
    GitCommitRecord(
        commit_sha="2222222222222222222222222222222222222222",
        occurred_at=BASE_TIMESTAMP + timedelta(minutes=20),
        author_name="Alice",
        author_email="alice@example.org",
        subject="docs: update README without ticket",
        branch="main",
        repository="worktrack",
    ),
)


class StaticGitLogRunner:
    """In-memory `GitLogRunner` test double returning preconfigured commits."""

    def __init__(self, commits: Sequence[GitCommitRecord]) -> None:
        """Initialize with static `GitCommitRecord` items."""
        self._commits = tuple(commits)

    def list_commits(
        self,
        repo_path: Path,
        *,
        author_pattern: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        max_commits: int = 500,
    ) -> Sequence[GitCommitRecord]:
        """Filter static commits by optional author substring and `[since, until)`."""
        _ = (repo_path, max_commits)
        return [
            item
            for item in self._commits
            if (not author_pattern or author_pattern.lower() in item.author_email.lower())
            and (since is None or item.occurred_at >= since)
            and (until is None or item.occurred_at < until)
        ]


class FakeForgeTransport:
    """In-memory `ForgeHttpTransport` recording request headers and serving static JSON."""

    def __init__(self, responses: Mapping[str, object]) -> None:
        """Initialize with URL-to-payload mapping."""
        self._responses = dict(responses)
        self.seen_headers: list[tuple[str, dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float = 15.0,
    ) -> ForgeHttpResponse:
        """Record headers and return canned `ForgeHttpResponse`."""
        _ = timeout_seconds
        self.seen_headers.append((url, dict(headers)))
        return ForgeHttpResponse(status_code=HTTP_STATUS_OK, payload=self._responses.get(url, []))


def init_git_repo_with_commits(repo_dir: Path) -> None:
    """Create a real local Git repository with commits across two authors and branches."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run_git(repo_dir, ["init", "-b", "main"])
    _run_git(repo_dir, ["config", "user.name", "Alice Developer"])
    _run_git(repo_dir, ["config", "user.email", "alice@example.org"])
    _commit_file(
        repo_dir,
        "README.md",
        "chore: bootstrap repo",
        "Alice Developer",
        "alice@example.org",
        BASE_TIMESTAMP - timedelta(hours=2),
    )
    _run_git(repo_dir, ["checkout", "-b", "feat/PROJ-101-oauth"])
    _commit_file(
        repo_dir,
        "auth.py",
        "feat(auth): wire broker callback\n\nRefs PROJ-101 and PROJ-202",
        "Alice Developer",
        "alice@example.org",
        BASE_TIMESTAMP + timedelta(minutes=30),
    )
    _commit_file(
        repo_dir,
        "other.py",
        "fix: bob commit on PROJ-999",
        "Bob Peer",
        "bob@example.org",
        BASE_TIMESTAMP + timedelta(minutes=45),
    )


def build_forge_canned_responses() -> dict[str, object]:
    """Build canned GitHub and GitLab commit/MR JSON payloads."""
    gh_commit = {
        "sha": "aaaa1111",
        "html_url": "https://github.com/acme/app/commit/aaaa1111",
        "commit": {
            "message": "fix: handle PKCE rotation (PROJ-301)",
            "author": {
                "name": "Alice",
                "email": "alice@example.org",
                "date": "2026-11-07T10:15:00Z",
            },
        },
    }
    gh_pull = {
        "number": 18,
        "title": "feat: add PROJ-302 dashboard",
        "body": "Implements PROJ-302",
        "updated_at": "2026-11-07T10:30:00Z",
        "html_url": "https://github.com/acme/app/pull/18",
        "user": {"login": "alice"},
        "head": {"ref": "feat/PROJ-302", "sha": "bbbb2222"},
    }
    gl_commit = {
        "id": "cccc3333",
        "title": "refactor: clean up PROJ-303 queries",
        "message": "refactor: clean up PROJ-303 queries",
        "author_name": "Alice",
        "author_email": "alice@example.org",
        "committed_date": "2026-11-07T11:00:00Z",
        "web_url": "https://gitlab.example.org/acme/core/-/commit/cccc3333",
    }
    gl_mr = {
        "iid": 9,
        "title": "Resolve PROJ-304 sensor rules",
        "description": "Closes PROJ-304",
        "source_branch": "feat/PROJ-304-sensors",
        "sha": "dddd4444",
        "updated_at": "2026-11-07T11:20:00Z",
        "web_url": "https://gitlab.example.org/acme/core/-/merge_requests/9",
        "author": {"username": "alice"},
    }
    return {
        "https://api.github.com/repos/acme/app/commits": [gh_commit],
        "https://api.github.com/repos/acme/app/pulls?state=all": [gh_pull],
        "https://gitlab.example.org/api/v4/projects/acme%2Fcore/repository/commits": [gl_commit],
        "https://gitlab.example.org/api/v4/projects/acme%2Fcore/merge_requests": [gl_mr],
    }


def _commit_file(
    repo_dir: Path,
    filename: str,
    message: str,
    author_name: str,
    author_email: str,
    timestamp: datetime,
) -> None:
    """Write a file and create a deterministic timestamped commit."""
    (repo_dir / filename).write_text(f"# {filename}\n", encoding="utf-8")
    _run_git(repo_dir, ["add", filename])
    iso_ts = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": iso_ts,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
        "GIT_COMMITTER_DATE": iso_ts,
    }
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", message],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_git(repo_dir: Path, args: Sequence[str]) -> None:
    """Execute a git command inside `repo_dir`."""
    subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=True,
        capture_output=True,
        text=True,
    )
