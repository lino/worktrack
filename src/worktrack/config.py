"""Layered XDG, TOML, and environment configuration loader for Worktrack."""

from __future__ import annotations

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# region Constants
APP_SLUG = "worktrack"
DEFAULT_CONFIG_FILENAME = "config.toml"
DEFAULT_DB_FILENAME = "ledger.sqlite3"
DEFAULT_SOCKET_FILENAME = "worktrackd.sock"
DEFAULT_TELEMETRY_SUBDIR = "telemetry"
DEFAULT_METRICS_HOST = "127.0.0.1"
DEFAULT_METRICS_PORT = 9112
DEFAULT_METRICS_SHUTDOWN_TIMEOUT = 5.0
MIN_TCP_PORT = 1
MAX_TCP_PORT = 65535
MIN_POSITIVE_LIMIT = 1
MIN_POSITIVE_TIMEOUT = 0.0
MIN_NON_NEGATIVE_DISTANCE = 0.0
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_DEPLOYMENT_MODE = "node"
DEFAULT_MAX_LABEL_VALUES = 20
DEFAULT_MAX_SERIES_PER_METRIC = 500
DEFAULT_REPORTING_PACK = "generic"
DEFAULT_HOLIDAY_SUBDIVISION = ""
DEFAULT_OFFICE_ONE_WAY_KM = 0.0

ALLOWED_DEPLOYMENT_MODES = frozenset({"node", "hub"})
ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

XDG_CONFIG_SEGMENTS = (".config",)
XDG_DATA_SEGMENTS = (".local", "share")
XDG_STATE_SEGMENTS = (".local", "state")
RUNTIME_SUBDIR_NAME = "run"

ENV_XDG_CONFIG_HOME = "XDG_CONFIG_HOME"
ENV_XDG_DATA_HOME = "XDG_DATA_HOME"
ENV_XDG_STATE_HOME = "XDG_STATE_HOME"
ENV_XDG_RUNTIME_DIR = "XDG_RUNTIME_DIR"

ENV_CONFIG_FILE = "WORKTRACK_CONFIG_FILE"
ENV_DB_PATH = "WORKTRACK_DB_PATH"
ENV_SOCKET_PATH = "WORKTRACK_SOCKET_PATH"
ENV_TELEMETRY_DIR = "WORKTRACK_TELEMETRY_DIR"
ENV_METRICS_HOST = "WORKTRACK_METRICS_HOST"
ENV_METRICS_PORT = "WORKTRACK_METRICS_PORT"
ENV_METRICS_SHUTDOWN_TIMEOUT = "WORKTRACK_METRICS_SHUTDOWN_TIMEOUT"
ENV_LOG_LEVEL = "WORKTRACK_LOG_LEVEL"
ENV_MODE = "WORKTRACK_MODE"
ENV_MAX_LABEL_VALUES = "WORKTRACK_MAX_LABEL_VALUES"
ENV_MAX_SERIES_PER_METRIC = "WORKTRACK_MAX_SERIES_PER_METRIC"
ENV_REPORTING_PACK = "WORKTRACK_REPORTING_PACK"
ENV_HOLIDAY_SUBDIVISION = "WORKTRACK_HOLIDAY_SUBDIVISION"
ENV_OFFICE_ONE_WAY_KM = "WORKTRACK_OFFICE_ONE_WAY_KM"
# endregion


# region Exceptions
class ConfigurationError(ValueError):
    """Raised when Worktrack configuration values or files fail validation."""


# endregion


# region Models
@dataclass(frozen=True, slots=True)
class WorktrackSettings:
    """Immutable runtime settings resolved across XDG, TOML, and env layers.

    Attributes:
        config_file: Resolved path to the TOML configuration file.
        db_path: Local SQLite ledger path for Node mode.
        socket_path: Local Unix domain socket path for `worktrackd` IPC.
        telemetry_dir: Directory storing persisted metrics and JSONL events.
        metrics_host: Loopback or bind address for `/metrics` HTTP exposition.
        metrics_port: TCP port for `/metrics` and `/healthz` HTTP exposition.
        metrics_shutdown_timeout: Graceful HTTP thread join timeout in seconds.
        log_level: Canonical uppercase logging level name.
        mode: Deployment topology (`node` or `hub`).
        max_label_values: Maximum distinct values permitted per metric label.
        max_series_per_metric: Maximum distinct label-tuple series per metric.
        reporting_pack: Slug identifier for the active reporting pack.
        holiday_subdivision: Regional subdivision code for public holidays.
        office_one_way_km: Distance in kilometers from home to primary office.
    """

    config_file: Path
    db_path: Path
    socket_path: Path
    telemetry_dir: Path
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: int = DEFAULT_METRICS_PORT
    metrics_shutdown_timeout: float = DEFAULT_METRICS_SHUTDOWN_TIMEOUT
    log_level: str = DEFAULT_LOG_LEVEL
    mode: str = DEFAULT_DEPLOYMENT_MODE
    max_label_values: int = DEFAULT_MAX_LABEL_VALUES
    max_series_per_metric: int = DEFAULT_MAX_SERIES_PER_METRIC
    reporting_pack: str = DEFAULT_REPORTING_PACK
    holiday_subdivision: str = DEFAULT_HOLIDAY_SUBDIVISION
    office_one_way_km: float = DEFAULT_OFFICE_ONE_WAY_KM

    def __post_init__(self) -> None:
        """Validate settings invariants upon instantiation."""
        _validate_settings(self)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        home_dir: Path | None = None,
    ) -> WorktrackSettings:
        """Resolve settings from XDG defaults, optional TOML file, and env vars."""
        env = os.environ if environ is None else environ
        resolved_home = Path.home() if home_dir is None else home_dir
        xdg = _resolve_xdg_dirs(env, resolved_home)

        default_cfg = xdg["config"] / APP_SLUG / DEFAULT_CONFIG_FILENAME
        raw_cfg = env.get(ENV_CONFIG_FILE, "").strip()
        config_file = Path(raw_cfg) if raw_cfg else default_cfg
        table = _load_toml_table(config_file)
        settings = _assemble_settings(config_file=config_file, xdg=xdg, env=env, table=table)
        _validate_settings(settings)
        return settings


# endregion


# region Validation
def _validate_settings(settings: WorktrackSettings) -> None:
    """Validate scalar ranges and enumerated values on `WorktrackSettings`."""
    if not settings.metrics_host.strip():
        raise ConfigurationError("metrics_host must be a non-empty hostname or IP.")
    if not (MIN_TCP_PORT <= settings.metrics_port <= MAX_TCP_PORT):
        raise ConfigurationError(f"metrics_port must be between {MIN_TCP_PORT} and {MAX_TCP_PORT}.")
    if (
        not math.isfinite(settings.metrics_shutdown_timeout)
        or settings.metrics_shutdown_timeout <= MIN_POSITIVE_TIMEOUT
    ):
        raise ConfigurationError("metrics_shutdown_timeout must be finite and > 0.")
    if settings.mode not in ALLOWED_DEPLOYMENT_MODES:
        allowed = ", ".join(sorted(ALLOWED_DEPLOYMENT_MODES))
        raise ConfigurationError(f"mode must be one of: {allowed}.")
    if settings.log_level not in ALLOWED_LOG_LEVELS:
        allowed = ", ".join(sorted(ALLOWED_LOG_LEVELS))
        raise ConfigurationError(f"log_level must be one of: {allowed}.")
    if (
        settings.max_label_values < MIN_POSITIVE_LIMIT
        or settings.max_series_per_metric < MIN_POSITIVE_LIMIT
    ):
        raise ConfigurationError("max_label_values and max_series_per_metric must be >= 1.")
    _validate_reporting_settings(settings)


def _validate_reporting_settings(settings: WorktrackSettings) -> None:
    """Validate reporting pack slug and commute distance invariants."""
    if not settings.reporting_pack.strip():
        raise ConfigurationError("reporting_pack must be a non-empty identifier.")
    if (
        not math.isfinite(settings.office_one_way_km)
        or settings.office_one_way_km < MIN_NON_NEGATIVE_DISTANCE
    ):
        raise ConfigurationError(
            f"office_one_way_km must be finite and >= {MIN_NON_NEGATIVE_DISTANCE}."
        )


# endregion


# region Resolution
def _assemble_reporting_settings(
    env: Mapping[str, str],
    table: Mapping[str, object],
) -> tuple[str, str, float]:
    """Resolve reporting pack, holiday subdivision, and commute km."""
    pack = _resolve_str_setting(
        ENV_REPORTING_PACK,
        "reporting_pack",
        DEFAULT_REPORTING_PACK,
        env,
        table,
        allow_empty=True,
    ).lower().strip()
    subdiv = _resolve_str_setting(
        ENV_HOLIDAY_SUBDIVISION, "holiday_subdivision", DEFAULT_HOLIDAY_SUBDIVISION, env, table
    ).upper().strip()
    km = _resolve_float_setting(
        ENV_OFFICE_ONE_WAY_KM, "office_one_way_km", DEFAULT_OFFICE_ONE_WAY_KM, env, table
    )
    return pack, subdiv, km


def _assemble_settings(
    *,
    config_file: Path,
    xdg: Mapping[str, Path],
    env: Mapping[str, str],
    table: Mapping[str, object],
) -> WorktrackSettings:
    """Assemble unvalidated `WorktrackSettings` from resolved layers."""
    default_db = xdg["data"] / APP_SLUG / DEFAULT_DB_FILENAME
    default_sock = xdg["runtime"] / APP_SLUG / DEFAULT_SOCKET_FILENAME
    default_tel = xdg["state"] / APP_SLUG / DEFAULT_TELEMETRY_SUBDIR
    pack, subdiv, km = _assemble_reporting_settings(env, table)
    return WorktrackSettings(
        config_file=config_file,
        db_path=_resolve_path_setting(ENV_DB_PATH, "db_path", default_db, env, table),
        socket_path=_resolve_path_setting(ENV_SOCKET_PATH, "socket_path", default_sock, env, table),
        telemetry_dir=_resolve_path_setting(
            ENV_TELEMETRY_DIR, "telemetry_dir", default_tel, env, table
        ),
        metrics_host=_resolve_str_setting(
            ENV_METRICS_HOST, "metrics_host", DEFAULT_METRICS_HOST, env, table
        ),
        metrics_port=_resolve_int_setting(
            ENV_METRICS_PORT, "metrics_port", DEFAULT_METRICS_PORT, env, table
        ),
        metrics_shutdown_timeout=_resolve_float_setting(
            ENV_METRICS_SHUTDOWN_TIMEOUT,
            "metrics_shutdown_timeout",
            DEFAULT_METRICS_SHUTDOWN_TIMEOUT,
            env,
            table,
        ),
        log_level=_resolve_str_setting(
            ENV_LOG_LEVEL, "log_level", DEFAULT_LOG_LEVEL, env, table
        ).upper(),
        mode=_resolve_str_setting(ENV_MODE, "mode", DEFAULT_DEPLOYMENT_MODE, env, table).lower(),
        max_label_values=_resolve_int_setting(
            ENV_MAX_LABEL_VALUES, "max_label_values", DEFAULT_MAX_LABEL_VALUES, env, table
        ),
        max_series_per_metric=_resolve_int_setting(
            ENV_MAX_SERIES_PER_METRIC,
            "max_series_per_metric",
            DEFAULT_MAX_SERIES_PER_METRIC,
            env,
            table,
        ),
        reporting_pack=pack,
        holiday_subdivision=subdiv,
        office_one_way_km=km,
    )


def _resolve_xdg_dirs(env: Mapping[str, str], home: Path) -> dict[str, Path]:
    """Compute XDG base directories relative to environment or synthetic home."""
    config_base = _env_path_or_default(env, ENV_XDG_CONFIG_HOME, home, XDG_CONFIG_SEGMENTS)
    data_base = _env_path_or_default(env, ENV_XDG_DATA_HOME, home, XDG_DATA_SEGMENTS)
    state_base = _env_path_or_default(env, ENV_XDG_STATE_HOME, home, XDG_STATE_SEGMENTS)
    raw_runtime = env.get(ENV_XDG_RUNTIME_DIR, "").strip()
    runtime_base = Path(raw_runtime) if raw_runtime else state_base / RUNTIME_SUBDIR_NAME
    return {
        "config": config_base,
        "data": data_base,
        "state": state_base,
        "runtime": runtime_base,
    }


def _env_path_or_default(
    env: Mapping[str, str],
    env_key: str,
    home: Path,
    relative_segments: tuple[str, ...],
) -> Path:
    """Read an XDG directory override from env or build from home segments."""
    raw_value = env.get(env_key, "").strip()
    return Path(raw_value) if raw_value else home.joinpath(*relative_segments)


def _load_toml_table(config_file: Path) -> dict[str, object]:
    """Parse an optional TOML configuration file and extract `[worktrack]` settings."""
    if not config_file.exists():
        return {}
    try:
        raw_doc = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Failed to parse config file {config_file}: {exc}") from exc

    scoped = raw_doc.get(APP_SLUG)
    if scoped is None:
        return dict(raw_doc)
    if not isinstance(scoped, dict):
        raise ConfigurationError(f"[{APP_SLUG}] section in {config_file} must be a table.")
    return dict(scoped)


def _resolve_path_setting(
    env_key: str,
    toml_key: str,
    default: Path,
    env: Mapping[str, str],
    table: Mapping[str, object],
) -> Path:
    """Resolve a filesystem `Path` setting with env > TOML > default precedence."""
    env_val = env.get(env_key, "").strip()
    if env_val:
        return Path(env_val)
    if toml_key in table:
        raw_val = table[toml_key]
        if not isinstance(raw_val, str) or not raw_val.strip():
            raise ConfigurationError(f"TOML key '{toml_key}' must be a non-empty string.")
        return Path(raw_val)
    return default


def _resolve_str_setting(
    env_key: str,
    toml_key: str,
    default: str,
    env: Mapping[str, str],
    table: Mapping[str, object],
    *,
    allow_empty: bool = False,
) -> str:
    """Resolve a string setting with env > TOML > default precedence."""
    if env_key in env:
        env_val = env[env_key]
        if allow_empty or env_val.strip():
            return env_val
    if toml_key in table:
        raw_val = table[toml_key]
        if not isinstance(raw_val, str):
            raise ConfigurationError(f"TOML key '{toml_key}' must be a string.")
        if allow_empty or raw_val.strip():
            return raw_val
    return default


def _resolve_int_setting(
    env_key: str,
    toml_key: str,
    default: int,
    env: Mapping[str, str],
    table: Mapping[str, object],
) -> int:
    """Resolve an integer setting with env > TOML > default precedence."""
    env_val = env.get(env_key, "").strip()
    if env_val:
        try:
            return int(env_val)
        except ValueError as exc:
            raise ConfigurationError(
                f"Environment variable {env_key} must be an integer, got '{env_val}'."
            ) from exc
    if toml_key in table:
        raw_val = table[toml_key]
        if isinstance(raw_val, bool) or not isinstance(raw_val, int):
            raise ConfigurationError(f"TOML key '{toml_key}' must be an integer.")
        return raw_val
    return default


def _resolve_float_setting(
    env_key: str,
    toml_key: str,
    default: float,
    env: Mapping[str, str],
    table: Mapping[str, object],
) -> float:
    """Resolve a float setting with env > TOML > default precedence."""
    env_val = env.get(env_key, "").strip()
    if env_val:
        try:
            return float(env_val)
        except ValueError as exc:
            raise ConfigurationError(
                f"Environment variable {env_key} must be a number, got '{env_val}'."
            ) from exc
    if toml_key in table:
        raw_val = table[toml_key]
        if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
            raise ConfigurationError(f"TOML key '{toml_key}' must be a number.")
        return float(raw_val)
    return default


def load_settings(
    environ: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> WorktrackSettings:
    """Load validated `WorktrackSettings` from environment, TOML, and XDG layers."""
    return WorktrackSettings.from_env(environ=environ, home_dir=home_dir)


# endregion
