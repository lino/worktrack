"""Grant-verified `JiraWorklogSink` posting ADF v1 worklogs with idempotency guards."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from worktrack.connectors.exceptions import JiraApiError, JiraConnectorError
from worktrack.connectors.jira_http import JiraOAuthClient
from worktrack.core.models import ensure_utc
from worktrack.outbox import (
    ApprovedOutboxGrant,
    OutboxItem,
    SinkWriteReceipt,
    enforce_outbox_grant,
)
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
JIRA_SINK_SLUG: Final = "jira"
ADF_SCHEMA_VERSION: Final = 1
IDEMPOTENCY_HEADER_NAME: Final = "Idempotency-Key"
WORKTRACK_IDEMPOTENCY_HEADER_NAME: Final = "X-Worktrack-Idempotency-Key"
JIRA_WORKLOG_PROPERTY_KEY: Final = "worktrack.egress"
MILLISECOND_DIGITS: Final = 3
DEFAULT_WORKLOG_HTTP_STATUS: Final = 200
# endregion


# region Jira Sink
def build_adf_comment_document(text: str) -> dict[str, object]:
    """Construct an Atlassian Document Format (ADF v1) single-paragraph comment."""
    safe_text = text.strip() or "Worktrack rollup"
    return {
        "type": "doc",
        "version": ADF_SCHEMA_VERSION,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": safe_text,
                    }
                ],
            }
        ],
    }


def format_jira_started_timestamp(timestamp: datetime) -> str:
    """Format a UTC `datetime` into Jira REST v3 worklog timestamp (`+0000` offset)."""
    utc_dt = ensure_utc(timestamp, field_name="started")
    millis = utc_dt.strftime("%f")[:MILLISECOND_DIGITS]
    return f"{utc_dt.strftime('%Y-%m-%dT%H:%M:%S')}.{millis}+0000"


class JiraWorklogSink:
    """Outbox-gated `RollupSink` writing ADF v1 worklogs to Jira Cloud REST API v3."""

    def __init__(
        self,
        *,
        client: JiraOAuthClient,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        """Initialize Jira worklog sink with `JiraOAuthClient` and optional telemetry."""
        self._client = client
        self._telemetry = telemetry

    @property
    def sink_name(self) -> str:
        """Return canonical Jira sink slug (`'jira'`)."""
        return JIRA_SINK_SLUG

    def write_rollup(
        self,
        item: OutboxItem,
        grant: ApprovedOutboxGrant | None = None,
        *,
        source: str = "direct_call",
    ) -> SinkWriteReceipt:
        """Verify `ApprovedOutboxGrant` and write idempotent ADF worklog to Jira Cloud."""
        enforce_outbox_grant(
            sink_name=self.sink_name,
            item=item,
            grant=grant,
            telemetry=self._telemetry,
            source=source,
        )
        encoded_issue = urllib.parse.quote(item.target_ref.strip(), safe="")
        worklog_path = f"/rest/api/3/issue/{encoded_issue}/worklog"

        if item.attempt_count > 0:
            recovered = self._find_existing_worklog_receipt(worklog_path, item.idempotency_key)
            if recovered is not None:
                return recovered

        response = self._client.request(
            "POST",
            worklog_path,
            json_body=_build_worklog_request_body(item),
            extra_headers={
                IDEMPOTENCY_HEADER_NAME: item.idempotency_key,
                WORKTRACK_IDEMPOTENCY_HEADER_NAME: item.idempotency_key,
            },
        )
        raw_id = response.payload.get("id")
        if raw_id is None or not str(raw_id).strip():
            raise JiraConnectorError(
                f"Jira worklog POST for '{item.target_ref}' omitted worklog 'id'.",
                http_status=response.status_code,
                destination_uri=self._client.build_url(worklog_path),
            )
        return SinkWriteReceipt(
            destination_uri=self._client.build_url(worklog_path),
            http_status=response.status_code,
            external_receipt_id=str(raw_id).strip(),
        )

    def _find_existing_worklog_receipt(
        self,
        worklog_path: str,
        idempotency_key: str,
    ) -> SinkWriteReceipt | None:
        """Check remote issue worklog properties on retries to prevent duplicate worklogs."""
        try:
            resp = self._client.request("GET", f"{worklog_path}?expand=properties")
        except JiraApiError:
            return None
        worklogs = resp.payload.get("worklogs")
        if not isinstance(worklogs, list):
            return None
        for entry in worklogs:
            receipt_id = _match_worklog_property(entry, idempotency_key)
            if receipt_id is not None:
                return SinkWriteReceipt(
                    destination_uri=self._client.build_url(worklog_path),
                    http_status=resp.status_code or DEFAULT_WORKLOG_HTTP_STATUS,
                    external_receipt_id=receipt_id,
                )
        return None


def _build_worklog_request_body(item: OutboxItem) -> dict[str, object]:
    """Build Jira REST v3 ADF worklog JSON body with `worktrack.egress` entity property."""
    return {
        "started": format_jira_started_timestamp(item.window_start),
        "timeSpentSeconds": item.rounded_duration_seconds,
        "comment": build_adf_comment_document(item.summary),
        "properties": [
            {
                "key": JIRA_WORKLOG_PROPERTY_KEY,
                "value": {
                    "idempotency_key": item.idempotency_key,
                    "outbox_item_id": item.id,
                },
            }
        ],
    }


def _match_worklog_property(entry: object, expected_key: str) -> str | None:
    """Return worklog `id` when `entry.properties` matches `expected_key`."""
    if not isinstance(entry, Mapping):
        return None
    props = entry.get("properties")
    if not isinstance(props, list):
        return None
    for prop in props:
        if not isinstance(prop, Mapping) or prop.get("key") != JIRA_WORKLOG_PROPERTY_KEY:
            continue
        val = prop.get("value")
        if isinstance(val, Mapping) and val.get("idempotency_key") == expected_key:
            raw_id = entry.get("id")
            if raw_id is not None and str(raw_id).strip():
                return str(raw_id).strip()
    return None


# endregion
