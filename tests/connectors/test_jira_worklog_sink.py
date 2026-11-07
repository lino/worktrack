"""Behavioural tests for `JiraWorklogSink` ADF worklogs, Outbox gate, and retry safety."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worktrack.connectors.jira import (
    ADF_SCHEMA_VERSION,
    IDEMPOTENCY_HEADER_NAME,
    JIRA_PROVIDER_SLUG,
    JIRA_SINK_SLUG,
    JIRA_WORKLOG_PROPERTY_KEY,
    JiraCloudConfig,
    JiraHttpResponse,
    JiraOAuthClient,
    JiraWorklogSink,
)
from worktrack.core import Ledger
from worktrack.outbox import (
    EGRESS_OUTCOME_ERROR,
    EGRESS_OUTCOME_OK,
    FLUSH_CADENCE_EOD,
    FLUSH_TRIGGER_MANUAL,
    GROUPING_WINDOW_DAY,
    OUTBOX_STATUS_FLUSHED,
    QUANTUM_FIFTEEN_MINUTES,
    EgressBypassBlockedError,
    OutboxDispatcher,
    OutboxItem,
    OutboxStore,
    RollupEngine,
    RollupPolicy,
)
from worktrack.secrets import OAuthTokenBundle, SecretValue, UserTokenStore
from worktrack.telemetry import TelemetryRecorder

# region Constants
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_GATEWAY_TIMEOUT = 504
FRESH_EXPIRY_SECONDS = 3_600
WORK_DURATION_SECONDS = 2_700
WORK_START = datetime(2026, 8, 6, 13, 0, 0, tzinfo=UTC)
# endregion


# region Fixtures
class _InMemoryKeyring:
    """In-memory `KeyringBackend` double for `JiraWorklogSink` tests."""

    def __init__(self) -> None:
        """Initialize empty keyring store."""
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        """Read stored password."""
        return self._store.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """Write password."""
        self._store[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        """Remove password."""
        self._store.pop((service_name, username), None)


class _WorklogTransport:
    """Injectable `JiraHttpTransport` capturing ADF worklog requests."""

    def __init__(self, responses: list[JiraHttpResponse]) -> None:
        """Initialize transport with queued HTTP responses."""
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float = 5.0,
    ) -> JiraHttpResponse:
        """Record call and pop next response."""
        _ = timeout_seconds
        self.calls.append(
            (method, url, dict(headers), None if json_body is None else dict(json_body))
        )
        return self._responses.pop(0)


def _build_jira_client(tmp_path: Path, transport: _WorklogTransport) -> JiraOAuthClient:
    """Create `JiraOAuthClient` seeded with a workstation user token."""
    token_store = UserTokenStore(
        keyring_backend=_InMemoryKeyring(), fallback_path=tmp_path / "tokens.json"
    )
    token_store.store(
        JIRA_PROVIDER_SLUG,
        OAuthTokenBundle(
            access_token=SecretValue("jira_worklog_token"), expires_in=FRESH_EXPIRY_SECONDS
        ),
    )
    return JiraOAuthClient(
        config=JiraCloudConfig(cloud_id="cloud-worklog-1"),
        token_store=token_store,
        transport=transport,
    )


def _stage_jira_worklog_item(
    ledger: Ledger,
    store: OutboxStore,
    issue_key: str,
    telemetry: TelemetryRecorder | None = None,
) -> OutboxItem:
    """Record a 45-minute interval with confidential notes and stage a Jira rollup item."""
    act = ledger.create_activity("Implement Jira Sink", external_ref=issue_key)
    ledger.record_structured_interval(
        act.id,
        started_at=WORK_START,
        ended_at=WORK_START + timedelta(seconds=WORK_DURATION_SECONDS),
        notes="Confidential internal debugging scratchpad",
    )
    return RollupEngine(ledger=ledger, outbox_store=store, telemetry=telemetry).stage_rollup(
        sink=JIRA_SINK_SLUG,
        policy=RollupPolicy(
            grouping_window=GROUPING_WINDOW_DAY,
            rounding_quantum_seconds=QUANTUM_FIFTEEN_MINUTES,
            redact_notes=True,
            flush_cadence=FLUSH_CADENCE_EOD,
        ),
    )[0]


# endregion


# region Jira Source & Sink Tests
def test_jira_worklog_sink_blocks_unapproved_direct_calls_and_posts_adf_worklog(
    tmp_path: Path,
) -> None:
    """Verify `JiraWorklogSink` blocks bypasses and posts ADF v1 worklogs when approved."""
    db_path = tmp_path / "jira_sink.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    transport = _WorklogTransport(
        [JiraHttpResponse(status_code=HTTP_CREATED, payload={"id": "90042"})]
    )
    sink = JiraWorklogSink(client=_build_jira_client(tmp_path, transport), telemetry=telemetry)

    with Ledger(db_path) as ledger, OutboxStore(db_path, telemetry=telemetry) as store:
        staged = _stage_jira_worklog_item(ledger, store, "PROJ-142", telemetry=telemetry)
        with pytest.raises(EgressBypassBlockedError):
            sink.write_rollup(staged, grant=None, source="direct_call")
        assert transport.calls == []

        store.approve_item(staged.id)
        dispatcher = OutboxDispatcher(outbox_store=store, sinks=[sink], telemetry=telemetry)
        flush_result = dispatcher.flush_item(staged.id, trigger=FLUSH_TRIGGER_MANUAL)
        assert flush_result.outcome == EGRESS_OUTCOME_OK
        assert flush_result.egress_record.external_receipt_id == "90042"
        assert store.get_item(staged.id).status == OUTBOX_STATUS_FLUSHED

        assert len(transport.calls) == 1
        method, url, headers, body = transport.calls[0]
        assert method == "POST" and url.endswith("/rest/api/3/issue/PROJ-142/worklog")
        assert headers[IDEMPOTENCY_HEADER_NAME] == staged.idempotency_key
        assert body is not None and body["timeSpentSeconds"] == WORK_DURATION_SECONDS
        assert str(body["started"]).endswith("+0000")
        assert body["comment"] == {
            "type": "doc",
            "version": ADF_SCHEMA_VERSION,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Implement Jira Sink"}]}
            ],
        }
        assert "Confidential internal debugging scratchpad" not in str(body)
        props = body["properties"]
        assert isinstance(props, list) and props[0]["key"] == JIRA_WORKLOG_PROPERTY_KEY
        assert props[0]["value"]["idempotency_key"] == staged.idempotency_key

    metrics_text = telemetry.render_metrics_text()
    assert (
        'worktrack_egress_bypass_blocked_total{sink="jira",source="direct_call"} 1.0'
        in metrics_text
    )
    assert (
        'worktrack_outbox_flushes_total{cadence="eod",outcome="ok",sink="jira"} 1.0' in metrics_text
    )


def test_jira_worklog_sink_recovers_remote_property_receipt_on_retry_after_timeout(
    tmp_path: Path,
) -> None:
    """Verify retries after ambiguous gateway errors recover existing worklog via property guard."""
    db_path = tmp_path / "jira_retry.sqlite3"
    with Ledger(db_path) as ledger, OutboxStore(db_path) as store:
        staged = _stage_jira_worklog_item(ledger, store, "PROJ-888")
        store.approve_item(staged.id)
        transport = _WorklogTransport(
            [
                JiraHttpResponse(
                    status_code=HTTP_GATEWAY_TIMEOUT,
                    payload={"errorMessages": ["Upstream timeout after commit"]},
                ),
                JiraHttpResponse(
                    status_code=HTTP_OK,
                    payload={
                        "worklogs": [
                            {
                                "id": "77701",
                                "properties": [
                                    {
                                        "key": JIRA_WORKLOG_PROPERTY_KEY,
                                        "value": {"idempotency_key": staged.idempotency_key},
                                    }
                                ],
                            }
                        ]
                    },
                ),
            ]
        )
        sink = JiraWorklogSink(client=_build_jira_client(tmp_path, transport))
        dispatcher = OutboxDispatcher(outbox_store=store, sinks=[sink])

        assert (
            dispatcher.flush_item(staged.id, trigger=FLUSH_TRIGGER_MANUAL).outcome
            == EGRESS_OUTCOME_ERROR
        )
        store.approve_item(staged.id)
        second = dispatcher.flush_item(staged.id, trigger=FLUSH_TRIGGER_MANUAL)
        assert (second.outcome, second.egress_record.external_receipt_id) == (
            EGRESS_OUTCOME_OK,
            "77701",
        )
        assert [call[0] for call in transport.calls] == ["POST", "GET"]


# endregion
