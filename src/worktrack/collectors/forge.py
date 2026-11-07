"""GitHub and GitLab `ForgeCommitSource` protocol, HTTP transport, and client."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, Self

from worktrack.collectors.forge_parsers import (
    matches_forge_filters,
    parse_github_commits_payload,
    parse_github_pulls_payload,
    parse_gitlab_commits_payload,
    parse_gitlab_merge_requests_payload,
)
from worktrack.collectors.models import (
    ForgeAuthenticationError,
    ForgeTransportError,
    GitCommitRecord,
    validate_clue_timestamp,
)
from worktrack.secrets import DEFAULT_TOKEN_ACCOUNT, UserTokenStore

# region Constants
FORGE_KIND_GITHUB: Final = "github"
FORGE_KIND_GITLAB: Final = "gitlab"
ALLOWED_FORGE_KINDS: Final[frozenset[str]] = frozenset({FORGE_KIND_GITHUB, FORGE_KIND_GITLAB})

DEFAULT_GITHUB_API_BASE_URL: Final = "https://api.github.com"
DEFAULT_GITLAB_API_BASE_URL: Final = "https://gitlab.com"
DEFAULT_FORGE_TIMEOUT_SECONDS: Final = 15.0

HTTPS_SCHEME: Final = "https"
HTTP_SCHEME: Final = "http"
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
JSON_ACCEPT_HEADER: Final = "application/json"

HTTP_STATUS_OK_MIN: Final = 200
HTTP_STATUS_OK_MAX: Final = 299
HTTP_STATUS_UNAUTHORIZED: Final = 401
HTTP_STATUS_FORBIDDEN: Final = 403
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class ForgeHttpResponse:
    """Decoded HTTP status and JSON payload returned by a forge API."""

    status_code: int
    payload: object


# endregion


# region Protocols
class ForgeHttpTransport(Protocol):
    """Injectable HTTP GET transport for GitHub and GitLab REST endpoints."""

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float = DEFAULT_FORGE_TIMEOUT_SECONDS,
    ) -> ForgeHttpResponse:
        """Execute an authenticated HTTP GET request and decode its JSON body."""
        ...


class ForgeCommitSource(Protocol):
    """Injectable protocol fetching commits and merge/pull requests from a forge."""

    def fetch_records(
        self,
        *,
        author_pattern: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Sequence[GitCommitRecord]:
        """Fetch normalized `GitCommitRecord` items matching optional author and window."""
        ...


# endregion


# region HTTP Transport
def validate_forge_url(url: str) -> None:
    """Require `https://` for remote forge hosts or loopback `http://` in local tests."""
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme == HTTPS_SCHEME and host:
        return
    if parsed.scheme == HTTP_SCHEME and host in LOOPBACK_HOSTS:
        return
    raise ForgeTransportError(
        "Forge API URL must use 'https://' (or loopback 'http://127.0.0.1' in local tests)."
    )


class UrllibForgeHttpTransport:
    """Default `urllib.request` implementation of `ForgeHttpTransport`."""

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float = DEFAULT_FORGE_TIMEOUT_SECONDS,
    ) -> ForgeHttpResponse:
        """Execute HTTP GET request via `urllib.request` and parse JSON response."""
        validate_forge_url(url)
        req_headers = {"Accept": JSON_ACCEPT_HEADER, **dict(headers)}
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                payload = json.loads(raw) if raw.strip() else []
                return ForgeHttpResponse(status_code=int(resp.status), payload=payload)
        except urllib.error.HTTPError as exc:
            return ForgeHttpResponse(status_code=int(exc.code), payload={})
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ForgeTransportError(f"Forge HTTP request failed for '{url}'.") from exc


# endregion


# region Forge Sources
class HttpForgeCommitSource:
    """GitHub and GitLab REST commit/MR `ForgeCommitSource` authenticated via `UserTokenStore`."""

    def __init__(
        self,
        *,
        forge_kind: str,
        project_slug: str,
        token_store: UserTokenStore,
        base_url: str | None = None,
        provider_name: str | None = None,
        account: str = DEFAULT_TOKEN_ACCOUNT,
        transport: ForgeHttpTransport | None = None,
        timeout_seconds: float = DEFAULT_FORGE_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize forge source with `UserTokenStore` and optional transport."""
        clean_kind = forge_kind.strip().lower()
        if clean_kind not in ALLOWED_FORGE_KINDS:
            raise ForgeTransportError(f"Unsupported forge_kind '{forge_kind}'.")
        clean_slug = project_slug.strip().strip("/")
        if not clean_slug:
            raise ForgeTransportError("project_slug must be a non-empty 'owner/repo' string.")
        default_base = (
            DEFAULT_GITHUB_API_BASE_URL
            if clean_kind == FORGE_KIND_GITHUB
            else DEFAULT_GITLAB_API_BASE_URL
        )
        resolved_base = (base_url or default_base).strip().rstrip("/")
        validate_forge_url(resolved_base)
        self._forge_kind = clean_kind
        self._project_slug = clean_slug
        self._base_url = resolved_base
        self._token_store = token_store
        self._provider_name = (provider_name or clean_kind).strip()
        self._account = account
        self._transport: ForgeHttpTransport = (
            UrllibForgeHttpTransport() if transport is None else transport
        )
        self._timeout_seconds = timeout_seconds

    @classmethod
    def for_github(
        cls,
        *,
        repository: str,
        token_store: UserTokenStore,
        base_url: str = DEFAULT_GITHUB_API_BASE_URL,
        provider_name: str = FORGE_KIND_GITHUB,
        account: str = DEFAULT_TOKEN_ACCOUNT,
        transport: ForgeHttpTransport | None = None,
    ) -> Self:
        """Construct a GitHub commit + pull-request `HttpForgeCommitSource`."""
        return cls(
            forge_kind=FORGE_KIND_GITHUB,
            project_slug=repository,
            token_store=token_store,
            base_url=base_url,
            provider_name=provider_name,
            account=account,
            transport=transport,
        )

    @classmethod
    def for_gitlab(
        cls,
        *,
        project: str,
        token_store: UserTokenStore,
        base_url: str = DEFAULT_GITLAB_API_BASE_URL,
        provider_name: str = FORGE_KIND_GITLAB,
        account: str = DEFAULT_TOKEN_ACCOUNT,
        transport: ForgeHttpTransport | None = None,
    ) -> Self:
        """Construct a GitLab commit + merge-request `HttpForgeCommitSource`."""
        return cls(
            forge_kind=FORGE_KIND_GITLAB,
            project_slug=project,
            token_store=token_store,
            base_url=base_url,
            provider_name=provider_name,
            account=account,
            transport=transport,
        )

    def fetch_records(
        self,
        *,
        author_pattern: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[GitCommitRecord]:
        """Authenticate via `UserTokenStore` and fetch commits and PRs/MRs."""
        utc_since = None if since is None else validate_clue_timestamp(since, field_name="since")
        utc_until = None if until is None else validate_clue_timestamp(until, field_name="until")
        headers = self._build_auth_headers()
        if self._forge_kind == FORGE_KIND_GITHUB:
            records = self._fetch_github_records(headers)
        else:
            records = self._fetch_gitlab_records(headers)
        return [
            rec
            for rec in records
            if matches_forge_filters(
                rec,
                author_pattern=author_pattern,
                since=utc_since,
                until=utc_until,
            )
        ]

    def _build_auth_headers(self) -> dict[str, str]:
        """Load bearer token from workstation-local `UserTokenStore`."""
        bundle = self._token_store.load(self._provider_name, self._account)
        if bundle is None:
            raise ForgeAuthenticationError(
                f"No OAuth token found in UserTokenStore for provider '{self._provider_name}'."
            )
        return {"Authorization": f"Bearer {bundle.access_token.expose()}"}

    def _fetch_github_records(self, headers: Mapping[str, str]) -> list[GitCommitRecord]:
        """Fetch and parse GitHub commits and pull requests."""
        commits_url = f"{self._base_url}/repos/{self._project_slug}/commits"
        pulls_url = f"{self._base_url}/repos/{self._project_slug}/pulls?state=all"
        commits_payload = self._get_array(commits_url, headers)
        pulls_payload = self._get_array(pulls_url, headers)
        return [
            *parse_github_commits_payload(commits_payload, repository=self._project_slug),
            *parse_github_pulls_payload(pulls_payload, repository=self._project_slug),
        ]

    def _fetch_gitlab_records(self, headers: Mapping[str, str]) -> list[GitCommitRecord]:
        """Fetch and parse GitLab commits and merge requests."""
        encoded_proj = urllib.parse.quote(self._project_slug, safe="")
        commits_url = f"{self._base_url}/api/v4/projects/{encoded_proj}/repository/commits"
        mrs_url = f"{self._base_url}/api/v4/projects/{encoded_proj}/merge_requests"
        commits_payload = self._get_array(commits_url, headers)
        mrs_payload = self._get_array(mrs_url, headers)
        return [
            *parse_gitlab_commits_payload(commits_payload, repository=self._project_slug),
            *parse_gitlab_merge_requests_payload(mrs_payload, repository=self._project_slug),
        ]

    def _get_array(self, url: str, headers: Mapping[str, str]) -> list[Mapping[str, object]]:
        """Execute GET request, verify status code, and return list of JSON objects."""
        resp = self._transport.get_json(url, headers=headers, timeout_seconds=self._timeout_seconds)
        if resp.status_code in (HTTP_STATUS_UNAUTHORIZED, HTTP_STATUS_FORBIDDEN):
            raise ForgeAuthenticationError(
                f"Forge authentication rejected with HTTP {resp.status_code} for '{url}'."
            )
        if not (HTTP_STATUS_OK_MIN <= resp.status_code <= HTTP_STATUS_OK_MAX):
            raise ForgeTransportError(f"Forge endpoint returned HTTP {resp.status_code}: '{url}'.")
        if not isinstance(resp.payload, list):
            return []
        return [item for item in resp.payload if isinstance(item, Mapping)]


# endregion
