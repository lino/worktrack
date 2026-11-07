"""Behavioural tests for SQLite WAL-mode migrations and connection lifecycle."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from worktrack.config import WorktrackSettings
from worktrack.core import (
    BUILTIN_WORKPLACE_FACET_KEY,
    BUILTIN_WORKPLACE_VALUES,
    DEFAULT_DB_DIR_MODE,
    DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    ENV_SQLITE_BUSY_TIMEOUT_MS,
    FACET_TYPE_ENUM,
    Ledger,
    LedgerValidationError,
    MigrationError,
)

# region Constants
CUSTOM_BUSY_TIMEOUT_MS = 12_500
ENV_BUSY_TIMEOUT_MS = 8_500
INITIAL_SCHEMA_VERSION = 1
PERMISSION_MASK = 0o777
# endregion


# region Migration Tests
def test_ledger_enables_wal_foreign_keys_busy_timeout_and_safe_dir_mode(
    tmp_path: Path,
) -> None:
    """Verify WAL mode, foreign keys, busy timeout, and safe parent permissions."""
    nested_dir = tmp_path / "nested_state" / "ledger_store"
    db_file = nested_dir / "ledger.sqlite3"

    with Ledger(db_file, busy_timeout_ms=CUSTOM_BUSY_TIMEOUT_MS) as ledger:
        assert ledger.db_path == db_file
        assert ledger.journal_mode.lower() == "wal"
        assert ledger.foreign_keys_enabled is True
        assert ledger.busy_timeout_ms == CUSTOM_BUSY_TIMEOUT_MS
        assert ledger.schema_version >= INITIAL_SCHEMA_VERSION

    if os.name == "posix":
        mode_bits = stat.S_IMODE(nested_dir.stat().st_mode) & PERMISSION_MASK
        assert mode_bits == DEFAULT_DB_DIR_MODE


def test_ledger_from_settings_and_env_busy_timeout_override(tmp_path: Path) -> None:
    """Verify Ledger resolves db_path from WorktrackSettings and env timeout."""
    synthetic_home = tmp_path / "synthetic_home"
    synthetic_home.mkdir(parents=True, exist_ok=True)
    xdg_data = tmp_path / "xdg_data"
    env = {
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg_cfg"),
        "XDG_RUNTIME_DIR": str(tmp_path / "xdg_run"),
        ENV_SQLITE_BUSY_TIMEOUT_MS: str(ENV_BUSY_TIMEOUT_MS),
    }
    settings = WorktrackSettings.from_env(environ=env, home_dir=synthetic_home)

    with Ledger.from_settings(settings, environ=env) as ledger:
        assert ledger.db_path == settings.db_path
        assert settings.db_path.is_file()
        assert ledger.busy_timeout_ms == ENV_BUSY_TIMEOUT_MS
        workplace = ledger.get_facet(BUILTIN_WORKPLACE_FACET_KEY)
        assert workplace.value_type == FACET_TYPE_ENUM
        assert workplace.allowed_values == BUILTIN_WORKPLACE_VALUES
        assert workplace.builtin is True

    with pytest.raises(LedgerValidationError):
        Ledger(tmp_path / "bad_timeout.sqlite3", busy_timeout_ms=-1)


def test_migrations_are_idempotent_and_detect_checksum_tampering(tmp_path: Path) -> None:
    """Verify reopening a migrated DB is idempotent and detects tampered checksums."""
    db_file = tmp_path / "migrated.sqlite3"

    with Ledger(db_file) as first_ledger:
        first_migrations = first_ledger.applied_migrations()
        assert len(first_migrations) >= INITIAL_SCHEMA_VERSION
        assert first_ledger.busy_timeout_ms == DEFAULT_SQLITE_BUSY_TIMEOUT_MS
        created = first_ledger.create_activity("Initial Task")

    with Ledger(db_file) as second_ledger:
        second_migrations = second_ledger.applied_migrations()
        assert second_migrations == first_migrations
        fetched = second_ledger.get_activity(created.id)
        assert fetched.name == "Initial Task"

    raw_conn = sqlite3.connect(db_file)
    try:
        raw_conn.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = ?",
            (INITIAL_SCHEMA_VERSION,),
        )
        raw_conn.commit()
    finally:
        raw_conn.close()

    with pytest.raises(MigrationError):
        Ledger(db_file)


# endregion
