"""Jira Cloud REST v3 JQL `Source` syncing `Activities`, `Entities`, and `Attributions`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from worktrack.connectors.exceptions import JiraConnectorError, JiraTransportError
from worktrack.connectors.jira_graph import (
    JIRA_SOURCE_SLUG as JIRA_SOURCE_SLUG,
)
from worktrack.connectors.jira_graph import (
    SyncCounters,
    upsert_issue_graph,
)
from worktrack.connectors.jira_http import JiraOAuthClient
from worktrack.core import Ledger
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
DEFAULT_JIRA_JQL: Final = "assignee = currentUser() AND statusCategory != Done"
ENV_JIRA_JQL: Final = "WORKTRACK_JIRA_JQL"

DEFAULT_JIRA_SEARCH_PATH: Final = "/rest/api/3/search/jql"
LEGACY_JIRA_SEARCH_PATH: Final = "/rest/api/3/search"
DEFAULT_JIRA_PAGE_SIZE: Final = 50
MAX_PAGINATION_PAGES: Final = 100

JIRA_SEARCH_FIELDS: Final[tuple[str, ...]] = (
    "summary",
    "status",
    "issuetype",
    "project",
    "components",
    "parent",
    "epic",
    "labels",
)

METRIC_SOURCE_SYNCS_TOTAL: Final = "worktrack_source_syncs_total"
OUTCOME_OK: Final = "ok"
OUTCOME_ERROR: Final = "error"
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class JiraSyncResult:
    """Summary counts returned by `JiraIssueSource.sync`."""

    jql: str
    issues_fetched: int
    activities_created: int
    activities_updated: int
    entities_created: int
    attributions_upserted: int


# endregion


# region Jira Source
class JiraIssueSource:
    """JQL `Source` syncing assigned Jira Cloud issues into local `Ledger`."""

    def __init__(
        self,
        *,
        client: JiraOAuthClient,
        default_jql: str | None = None,
        search_path: str = DEFAULT_JIRA_SEARCH_PATH,
        page_size: int = DEFAULT_JIRA_PAGE_SIZE,
        telemetry: TelemetryRecorder | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize JQL issue source with `JiraOAuthClient` and optional telemetry."""
        self._client = client
        self._default_jql = _resolve_default_jql(default_jql, environ)
        self._search_path = search_path
        self._page_size = max(1, page_size)
        self._telemetry = telemetry

    def sync(self, ledger: Ledger, *, jql: str | None = None) -> JiraSyncResult:
        """Query Jira Cloud JQL and upsert Targeted `Activities`, `Entities`, and `Attributions`."""
        resolved_jql = (jql or self._default_jql).strip()
        if not resolved_jql:
            self._record_sync_metric(OUTCOME_ERROR)
            raise JiraConnectorError("JQL query must be a non-empty string.")
        try:
            issues = self._fetch_all_issues(resolved_jql)
            counters = SyncCounters(issues_fetched=len(issues))
            for raw_issue in issues:
                upsert_issue_graph(ledger, raw_issue, counters)
        except Exception:
            self._record_sync_metric(OUTCOME_ERROR)
            raise

        self._record_sync_metric(OUTCOME_OK)
        return JiraSyncResult(
            jql=resolved_jql,
            issues_fetched=counters.issues_fetched,
            activities_created=counters.activities_created,
            activities_updated=counters.activities_updated,
            entities_created=counters.entities_created,
            attributions_upserted=counters.attributions_upserted,
        )

    def _fetch_all_issues(self, jql: str) -> list[Mapping[str, object]]:
        """Paginate through `/rest/api/3/search/jql` (or legacy `startAt`/`total`)."""
        collected: list[Mapping[str, object]] = []
        next_token: str | None = None
        start_at = 0
        for _ in range(MAX_PAGINATION_PAGES):
            body = _build_search_body(jql, self._page_size, next_token, start_at)
            resp = self._client.request("POST", self._search_path, json_body=body)
            page_issues = _extract_issues_array(resp.payload)
            collected.extend(page_issues)
            next_token, start_at, keep_going = _next_pagination_cursor(
                resp.payload,
                current_token=next_token,
                collected_count=len(collected),
                has_items=bool(page_issues),
            )
            if not keep_going:
                break
        return collected

    def _record_sync_metric(self, outcome: str) -> None:
        """Increment `worktrack_source_syncs_total{source='jira', outcome=...}`."""
        if self._telemetry is not None:
            self._telemetry.increment_counter(
                METRIC_SOURCE_SYNCS_TOTAL,
                {"source": JIRA_SOURCE_SLUG, "outcome": outcome},
            )


def _resolve_default_jql(
    explicit_jql: str | None,
    environ: Mapping[str, str] | None,
) -> str:
    """Resolve default JQL from argument, `WORKTRACK_JIRA_JQL`, or built-in default."""
    if explicit_jql is not None and explicit_jql.strip():
        return explicit_jql.strip()
    env = os.environ if environ is None else environ
    env_jql = env.get(ENV_JIRA_JQL, "").strip()
    return env_jql if env_jql else DEFAULT_JIRA_JQL


def _build_search_body(
    jql: str,
    page_size: int,
    next_token: str | None,
    start_at: int,
) -> dict[str, object]:
    """Assemble JSON request body for Jira REST v3 JQL search."""
    body: dict[str, object] = {
        "jql": jql,
        "fields": list(JIRA_SEARCH_FIELDS),
        "maxResults": page_size,
    }
    if next_token is not None:
        body["nextPageToken"] = next_token
    elif start_at > 0:
        body["startAt"] = start_at
    return body


def _next_pagination_cursor(
    payload: Mapping[str, object],
    *,
    current_token: str | None,
    collected_count: int,
    has_items: bool,
) -> tuple[str | None, int, bool]:
    """Determine `(next_token, next_start_at, should_continue)` from a search page."""
    raw_next = payload.get("nextPageToken")
    if isinstance(raw_next, str) and raw_next.strip() and payload.get("isLast") is not True:
        return raw_next.strip(), 0, True
    total = payload.get("total")
    if current_token is None and isinstance(total, int) and has_items and collected_count < total:
        return None, collected_count, True
    return None, 0, False


def _extract_issues_array(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Validate and return `issues` list from Jira search JSON response."""
    raw_issues = payload.get("issues", [])
    if not isinstance(raw_issues, list):
        raise JiraTransportError("Jira search payload 'issues' field must be a JSON array.")
    return [item for item in raw_issues if isinstance(item, Mapping)]


# endregion
