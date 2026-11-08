"""Unit and behavioural tests for Worktrack configuration loader and settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from worktrack.config import (
    DEFAULT_DEPLOYMENT_MODE,
    DEFAULT_HOLIDAY_SUBDIVISION,
    DEFAULT_LOG_LEVEL,
    DEFAULT_OFFICE_ONE_WAY_KM,
    DEFAULT_REPORTING_PACK,
    ENV_HOLIDAY_SUBDIVISION,
    ENV_OFFICE_ONE_WAY_KM,
    ENV_REPORTING_PACK,
    ConfigurationError,
    WorktrackSettings,
    load_settings,
)

# region Constants
SAMPLE_CUSTOM_KM = 24.5
SAMPLE_NEGATIVE_KM = -5.0
# endregion


# region Tests
def test_worktrack_settings_reporting_pack_defaults(tmp_path: Path) -> None:
    """Verify default values for reporting_pack, holiday_subdivision, and office_one_way_km."""
    settings = WorktrackSettings.from_env(environ={}, home_dir=tmp_path)
    assert settings.reporting_pack == DEFAULT_REPORTING_PACK
    assert settings.holiday_subdivision == DEFAULT_HOLIDAY_SUBDIVISION
    assert settings.office_one_way_km == DEFAULT_OFFICE_ONE_WAY_KM
    assert settings.mode == DEFAULT_DEPLOYMENT_MODE
    assert settings.log_level == DEFAULT_LOG_LEVEL


def test_worktrack_settings_reporting_pack_env_overrides(tmp_path: Path) -> None:
    """Verify environment variables override reporting and commute settings."""
    env = {
        ENV_REPORTING_PACK: " DE ",
        ENV_HOLIDAY_SUBDIVISION: " by ",
        ENV_OFFICE_ONE_WAY_KM: "32.5",
    }
    settings = load_settings(environ=env, home_dir=tmp_path)
    assert settings.reporting_pack == "de"
    assert settings.holiday_subdivision == "BY"
    assert settings.office_one_way_km == 32.5


def test_worktrack_settings_reporting_pack_toml_layer(tmp_path: Path) -> None:
    """Verify TOML file values populate reporting settings when env is unset."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
        [worktrack]
        reporting_pack = "at"
        holiday_subdivision = "w"
        office_one_way_km = 18.0
        """,
        encoding="utf-8",
    )
    env = {"WORKTRACK_CONFIG_FILE": str(cfg_file)}
    settings = WorktrackSettings.from_env(environ=env, home_dir=tmp_path)
    assert settings.reporting_pack == "at"
    assert settings.holiday_subdivision == "W"
    assert settings.office_one_way_km == 18.0


def test_worktrack_settings_validation_rejects_empty_pack(tmp_path: Path) -> None:
    """Verify validation fails when reporting_pack is empty or blank."""
    with pytest.raises(ConfigurationError, match="reporting_pack"):
        WorktrackSettings.from_env(
            environ={ENV_REPORTING_PACK: "   "},
            home_dir=tmp_path,
        )


def test_worktrack_settings_validation_rejects_negative_commute_km(tmp_path: Path) -> None:
    """Verify validation fails when office_one_way_km is negative."""
    with pytest.raises(ConfigurationError, match="office_one_way_km"):
        WorktrackSettings.from_env(
            environ={ENV_OFFICE_ONE_WAY_KM: str(SAMPLE_NEGATIVE_KM)},
            home_dir=tmp_path,
        )


def test_worktrack_settings_validation_rejects_non_finite_commute_km(tmp_path: Path) -> None:
    """Verify validation fails when office_one_way_km is NaN or Infinity."""
    with pytest.raises(ConfigurationError, match="office_one_way_km"):
        WorktrackSettings(
            config_file=tmp_path / "config.toml",
            db_path=tmp_path / "db.sqlite3",
            socket_path=tmp_path / "sock",
            telemetry_dir=tmp_path / "telemetry",
            office_one_way_km=float("nan"),
        )
# endregion
