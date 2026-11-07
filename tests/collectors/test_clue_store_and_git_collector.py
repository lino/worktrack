"""Behavioural tests for `ClueStore`, `GitClueCollector`, and `ForgeCommitSource`."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from worktrack.agenda import AgendaPlanner
from worktrack.collectors import (
    CLUE_SOURCE_FORGE,
    CLUE_SOURCE_GIT,
    CLUE_SOURCE_SENSOR,
    Clue,
    ClueMigrationError,
    ClueNotFoundError,
    ClueStore,
    ClueValidationError,
    CollectorError,
    ForgeAuthenticationError,
    GitClueCollector,
    HttpForgeCommitSource,
    SubprocessGitLogRunner,
)
from worktrack.config import WorktrackSettings
from worktrack.core import Ledger
from worktrack.outbox import OutboxStore
from worktrack.secrets import OAuthTokenBundle, SecretValue, UserTokenStore
from worktrack.telemetry import TelemetryRecorder

from . import (
    BASE_TIMESTAMP,
    SAMPLE_STATIC_COMMITS,
    FakeForgeTransport,
    StaticGitLogRunner,
    build_forge_canned_responses,
    init_git_repo_with_commits,
)

# region Constants
EXPECTED_INITIAL_SCHEMA_VERSION = 1
CONFIDENCE_HIGH = 0.92
CONFIDENCE_MEDIUM = 0.75
CONFIDENCE_UPDATED = 0.98
INVALID_CONFIDENCE_HIGH = 1.5
INVALID_CONFIDENCE_LOW = -0.1
EXPECTED_TWO_CLUES = 2
EXPECTED_FOUR_CLUES = 4
SAMPLE_GITHUB_TOKEN = "gho_test_secret_token_value_12345"
SAMPLE_GITLAB_TOKEN = "glpat_test_secret_token_value_67890"
# endregion


# region Clue Store Tests
def test_clue_store_coexists_with_ledger_agenda_and_outbox_migrations(tmp_path: Path) -> None:
    """Verify `clue_schema_migrations` coexists in `settings.db_path` and checks integrity."""
    settings = WorktrackSettings.from_env(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home_dir=tmp_path,
    )
    with (
        ClueStore.from_settings(settings) as clue_store,
        Ledger.from_settings(settings) as ledger,
        AgendaPlanner.from_settings(settings, ledger=ledger) as planner,
        OutboxStore.from_settings(settings) as outbox,
    ):
        assert clue_store.clue_schema_version == EXPECTED_INITIAL_SCHEMA_VERSION
        assert ledger.schema_version == EXPECTED_INITIAL_SCHEMA_VERSION
        assert planner.agenda_schema_version == EXPECTED_INITIAL_SCHEMA_VERSION
        assert outbox.outbox_schema_version == EXPECTED_INITIAL_SCHEMA_VERSION
        applied = clue_store.applied_migrations()
        assert len(applied) == EXPECTED_INITIAL_SCHEMA_VERSION
        assert applied[0].name == "0001_initial_clues_table"

    with contextlib.closing(sqlite3.connect(settings.db_path)) as raw_conn:
        raw_conn.execute(
            "UPDATE clue_schema_migrations SET checksum = 'tampered' WHERE version = 1"
        )
        raw_conn.commit()

    with pytest.raises(ClueMigrationError, match="Checksum mismatch"):
        ClueStore.from_settings(settings)


def test_clue_store_idempotent_upsert_filters_and_zero_egress(tmp_path: Path) -> None:
    """Verify `(source, external_key)` idempotency, window/key filters, and zero egress."""
    db_path = tmp_path / "clues.sqlite3"
    with ClueStore(db_path) as store, OutboxStore(db_path) as outbox:
        first = store.upsert_clue(
            source=CLUE_SOURCE_GIT,
            external_key="repo:abc1234",
            occurred_at=BASE_TIMESTAMP,
            summary="feat: initial PROJ-123 commit",
            inferred_activity_key="PROJ-123",
            confidence=CONFIDENCE_MEDIUM,
            metadata={"branch": "feat/PROJ-123"},
        )
        updated = store.upsert_clue(
            source=CLUE_SOURCE_GIT,
            external_key="repo:abc1234",
            occurred_at=BASE_TIMESTAMP + timedelta(minutes=5),
            summary="feat: amended PROJ-123 commit",
            inferred_activity_key="PROJ-123",
            activity_id="act-123",
            confidence=CONFIDENCE_UPDATED,
            metadata={"branch": "feat/PROJ-123", "amended": True},
        )
        assert (updated.id, updated.created_at) == (first.id, first.created_at)
        assert updated.summary == "feat: amended PROJ-123 commit"
        assert (updated.confidence, updated.activity_id) == (CONFIDENCE_UPDATED, "act-123")
        assert updated.metadata["amended"] is True

        sensor_clue = store.upsert_clue(
            source=CLUE_SOURCE_SENSOR,
            external_key="workplace:2026-11-07:home",
            occurred_at=BASE_TIMESTAMP + timedelta(hours=1),
            summary="Inferred home_office via Wi-Fi",
            confidence=CONFIDENCE_HIGH,
            metadata={"workplace": "home_office"},
        )
        assert isinstance(sensor_clue, Clue)
        assert len(store.list_clues()) == EXPECTED_TWO_CLUES
        assert len(store.list_clues(source=CLUE_SOURCE_GIT)) == 1
        assert len(store.list_clues(inferred_activity_key="PROJ-123")) == 1
        assert len(store.list_clues(activity_id="act-123")) == 1
        window_clues = store.list_clues(
            since=BASE_TIMESTAMP,
            until=BASE_TIMESTAMP + timedelta(minutes=30),
        )
        assert len(window_clues) == 1
        assert store.get_clue(first.id).external_key == "repo:abc1234"
        assert store.find_clue(CLUE_SOURCE_GIT, "repo:abc1234") is not None
        assert (outbox.list_items(), outbox.list_egress_records()) == ([], [])
        assert store.delete_clue(sensor_clue.id) is True
        with pytest.raises(ClueNotFoundError):
            store.get_clue(sensor_clue.id)


def test_clue_store_validates_inputs(tmp_path: Path) -> None:
    """Verify `ClueStore` rejects naive timestamps, invalid confidence, and blank keys."""
    with ClueStore(tmp_path / "validation.sqlite3") as store:
        with pytest.raises(ClueValidationError):
            store.upsert_clue(
                source=CLUE_SOURCE_GIT,
                external_key="k1",
                occurred_at=datetime(2026, 11, 7, 9, 0, 0),
                summary="naive timestamp",
            )
        for bad_conf in (INVALID_CONFIDENCE_LOW, INVALID_CONFIDENCE_HIGH):
            with pytest.raises(ClueValidationError):
                store.upsert_clue(
                    source=CLUE_SOURCE_GIT,
                    external_key="k2",
                    occurred_at=BASE_TIMESTAMP,
                    summary="bad confidence",
                    confidence=bad_conf,
                )
        with pytest.raises(ClueValidationError):
            store.upsert_clue(
                source="   ",
                external_key="k3",
                occurred_at=BASE_TIMESTAMP,
                summary="blank source",
            )


# endregion


# region Git Collector Tests
def test_subprocess_git_log_runner_scans_real_git_repo_with_author_and_window(
    tmp_path: Path,
) -> None:
    """Verify `SubprocessGitLogRunner` filters real `git log` commits by author and window."""
    repo_dir = tmp_path / "sample_repo"
    init_git_repo_with_commits(repo_dir)
    runner = SubprocessGitLogRunner()

    alice_window = runner.list_commits(
        repo_dir,
        author_pattern="alice@example.org",
        since=BASE_TIMESTAMP,
        until=BASE_TIMESTAMP + timedelta(hours=1),
    )
    assert len(alice_window) == 1
    assert alice_window[0].author_email == "alice@example.org"
    assert "PROJ-101" in alice_window[0].message

    clues_db = tmp_path / "clues.sqlite3"
    with ClueStore(clues_db) as clue_store, Ledger(clues_db) as ledger:
        act = ledger.create_activity("OAuth Callback", external_ref="PROJ-101", source="jira")
        collector = GitClueCollector(
            clue_store=clue_store,
            ledger=ledger,
            repo_paths=(repo_dir,),
            author_pattern="alice@example.org",
        )
        summary = collector.sync(
            since=BASE_TIMESTAMP,
            until=BASE_TIMESTAMP + timedelta(hours=1),
        )
        assert (summary.commits_scanned, summary.clues_upserted, summary.activities_linked) == (
            1,
            1,
            1,
        )
        assert summary.clues[0].inferred_activity_key == "PROJ-101"
        assert summary.clues[0].activity_id == act.id
        assert summary.clues[0].metadata["matched_keys"] == ["PROJ-101", "PROJ-202"]


def test_git_clue_collector_extracts_keys_links_ledger_activities_and_records_metrics(
    tmp_path: Path,
) -> None:
    """Verify custom regex extraction, `Activity` linking, deduplication, and metrics."""
    db_path = tmp_path / "node.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    with Ledger(db_path, telemetry=telemetry) as ledger, ClueStore(db_path) as clue_store:
        linked_act = ledger.create_activity("Clue Collector", external_ref="WT-42", source="jira")
        collector = GitClueCollector(
            clue_store=clue_store,
            ledger=ledger,
            repo_paths=(tmp_path / "virtual_repo",),
            git_runner=StaticGitLogRunner(SAMPLE_STATIC_COMMITS),
            issue_key_pattern=r"\b(WT-\d+)\b",
            telemetry=telemetry,
        )
        first_sync = collector.sync(
            author_pattern="alice@example.org",
            since=BASE_TIMESTAMP,
            until=BASE_TIMESTAMP + timedelta(hours=1),
        )
        second_sync = collector.sync(
            author_pattern="alice@example.org",
            since=BASE_TIMESTAMP,
            until=BASE_TIMESTAMP + timedelta(hours=1),
        )
        assert first_sync.commits_scanned == EXPECTED_TWO_CLUES
        assert first_sync.activities_linked == 1
        assert second_sync.clues_upserted == EXPECTED_TWO_CLUES
        persisted = clue_store.list_clues(source=CLUE_SOURCE_GIT)
        assert len(persisted) == EXPECTED_TWO_CLUES
        wt_clue = next(item for item in persisted if item.inferred_activity_key == "WT-42")
        assert wt_clue.activity_id == linked_act.id

    assert (
        'worktrack_source_syncs_total{outcome="ok",source="git"} 2.0'
        in telemetry.render_metrics_text()
    )


# endregion


# region Forge Collector Tests
def test_forge_commit_sources_github_and_gitlab_via_user_token_store(tmp_path: Path) -> None:
    """Verify GitHub & GitLab commit/MR `ForgeCommitSource` sync via `UserTokenStore`."""
    db_path = tmp_path / "forge.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    token_store = UserTokenStore(fallback_path=tmp_path / "tokens.json")
    token_store.store("github", OAuthTokenBundle(access_token=SecretValue(SAMPLE_GITHUB_TOKEN)))
    token_store.store("gitlab", OAuthTokenBundle(access_token=SecretValue(SAMPLE_GITLAB_TOKEN)))
    transport = FakeForgeTransport(build_forge_canned_responses())

    gh_source = HttpForgeCommitSource.for_github(
        repository="acme/app",
        token_store=token_store,
        transport=transport,
    )
    gl_source = HttpForgeCommitSource.for_gitlab(
        project="acme/core",
        base_url="https://gitlab.example.org",
        token_store=token_store,
        transport=transport,
    )
    with Ledger(db_path) as ledger, ClueStore(db_path) as clue_store:
        act_301 = ledger.create_activity("PKCE Rotation", external_ref="PROJ-301", source="jira")
        act_304 = ledger.create_activity("Sensor Rules", external_ref="PROJ-304", source="jira")
        collector = GitClueCollector(
            clue_store=clue_store,
            ledger=ledger,
            forge_sources=(gh_source, gl_source),
            telemetry=telemetry,
        )
        summary = collector.sync(
            since=BASE_TIMESTAMP,
            until=BASE_TIMESTAMP + timedelta(hours=4),
        )
        assert summary.forge_records_scanned == EXPECTED_FOUR_CLUES
        assert summary.activities_linked == EXPECTED_TWO_CLUES
        by_key = {
            c.inferred_activity_key: c for c in clue_store.list_clues(source=CLUE_SOURCE_FORGE)
        }
        assert by_key["PROJ-301"].activity_id == act_301.id
        assert by_key["PROJ-304"].activity_id == act_304.id

    auth_headers = [hdrs["Authorization"] for _, hdrs in transport.seen_headers]
    assert f"Bearer {SAMPLE_GITHUB_TOKEN}" in auth_headers
    assert f"Bearer {SAMPLE_GITLAB_TOKEN}" in auth_headers


def test_git_clue_collector_records_error_metric_on_missing_token_or_runner_failure(
    tmp_path: Path,
) -> None:
    """Verify `worktrack_source_syncs_total{outcome='error',source='git'}` on failure."""
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    empty_token_store = UserTokenStore(fallback_path=tmp_path / "empty_tokens.json")
    gh_source = HttpForgeCommitSource.for_github(
        repository="acme/app",
        token_store=empty_token_store,
        transport=FakeForgeTransport({}),
    )
    with ClueStore(tmp_path / "err.sqlite3") as clue_store:
        collector = GitClueCollector(
            clue_store=clue_store,
            forge_sources=(gh_source,),
            telemetry=telemetry,
        )
        with pytest.raises(ForgeAuthenticationError):
            collector.sync()

        bad_repo_collector = GitClueCollector(
            clue_store=clue_store,
            repo_paths=(tmp_path / "nonexistent_git_repo",),
            telemetry=telemetry,
        )
        with pytest.raises(CollectorError):
            bad_repo_collector.sync()

    assert (
        'worktrack_source_syncs_total{outcome="error",source="git"} 2.0'
        in telemetry.render_metrics_text()
    )


# endregion
