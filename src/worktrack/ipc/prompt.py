"""Porcelain shell prompt formatter and non-blocking `worktrackd` status probe."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from worktrack.config import ConfigurationError
from worktrack.ipc.client import (
    DEFAULT_PROMPT_TIMEOUT_SECONDS,
    IpcClient,
    IpcEndpointConfig,
    resolve_ipc_endpoint,
)
from worktrack.ipc.models import DaemonStatusSnapshot, IpcError
from worktrack.ipc.server import LOOPBACK_IPV4

# region Constants
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
SECONDS_PER_HOUR = SECONDS_PER_MINUTE * MINUTES_PER_HOUR
ZERO_SECONDS = 0
EMPTY_PROMPT_SEGMENT = ""
# endregion


# region Prompt Formatting
def format_duration_compact(seconds: int) -> str:
    """Format elapsed seconds into compact shell-friendly units (`42s`, `25m`, `2h30m`)."""
    clamped = max(ZERO_SECONDS, seconds)
    if clamped == ZERO_SECONDS:
        return "0m"
    if clamped < SECONDS_PER_MINUTE:
        return f"{clamped}s"
    hours, rem_seconds = divmod(clamped, SECONDS_PER_HOUR)
    minutes = rem_seconds // SECONDS_PER_MINUTE
    if hours == ZERO_SECONDS:
        return f"{minutes}m"
    if minutes == ZERO_SECONDS:
        return f"{hours}h"
    return f"{hours}h{minutes}m"


def format_porcelain_status(snapshot: DaemonStatusSnapshot | None) -> str:
    """Render single-line porcelain prompt status or `""` when idle/offline.

    Args:
        snapshot: Current `DaemonStatusSnapshot` or `None` when `worktrackd` is offline.

    Returns:
        Single-line porcelain status (`<activity> <elapsed> | today <daily>`) when
        actively tracking an `Interval`, or `""` when idle or offline.
    """
    if snapshot is None or not snapshot.is_tracking:
        return EMPTY_PROMPT_SEGMENT
    label = snapshot.active_activity_key or snapshot.active_activity_title or ""
    if not label:
        return EMPTY_PROMPT_SEGMENT
    elapsed = format_duration_compact(snapshot.active_interval_elapsed_seconds)
    daily = format_duration_compact(snapshot.daily_total_seconds)
    return f"{label} {elapsed} | today {daily}"


def probe_prompt_status(
    *,
    socket_path: Path | None = None,
    tcp_host: str = LOOPBACK_IPV4,
    tcp_port: int | None = None,
    auth_token: str | None = None,
    timeout_seconds: float = DEFAULT_PROMPT_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> str:
    """Probe `worktrackd` with tight timeout and never raise when offline or idle."""
    try:
        endpoint = _select_probe_endpoint(
            socket_path=socket_path,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            auth_token=auth_token,
            timeout_seconds=timeout_seconds,
            environ=environ,
            home_dir=home_dir,
        )
        snapshot = IpcClient(endpoint).status()
    except (IpcError, ConfigurationError, ValueError, OSError):
        return EMPTY_PROMPT_SEGMENT
    return format_porcelain_status(snapshot)


def _select_probe_endpoint(
    *,
    socket_path: Path | None,
    tcp_host: str,
    tcp_port: int | None,
    auth_token: str | None,
    timeout_seconds: float,
    environ: Mapping[str, str] | None,
    home_dir: Path | None,
) -> IpcEndpointConfig:
    """Construct probe `IpcEndpointConfig` from explicit overrides or environment."""
    if socket_path is not None:
        return IpcEndpointConfig.for_unix(socket_path, timeout_seconds=timeout_seconds)
    if tcp_port is not None:
        return IpcEndpointConfig.for_tcp(
            tcp_host,
            tcp_port,
            auth_token=auth_token,
            timeout_seconds=timeout_seconds,
        )
    return resolve_ipc_endpoint(
        environ=environ,
        home_dir=home_dir,
        default_timeout_seconds=timeout_seconds,
    )


# endregion
