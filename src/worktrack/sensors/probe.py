"""Injectable `WorkstationProbe` protocol and safe default `LocalWorkstationProbe`."""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from worktrack.sensors.models import (
    ENV_SENSOR_DISPLAY_COUNT,
    ENV_SENSOR_INTERFACES,
    ENV_SENSOR_IP_ADDRESSES,
    ENV_SENSOR_WIFI_SSID,
    MIN_DISPLAY_COUNT,
    SensorValidationError,
    WorkstationSnapshot,
)


# region Protocols
class WorkstationProbe(Protocol):
    """Injectable protocol probing local workstation network and display topology."""

    def probe(self) -> WorkstationSnapshot:
        """Capture a point-in-time `WorkstationSnapshot` without external network egress."""
        ...


# endregion


# region Workstation Probe
class LocalWorkstationProbe:
    """Safe default `WorkstationProbe` inspecting local interfaces and env overrides."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize local probe with optional environment mapping and clock function."""
        self._environ = environ
        self._now_fn: Callable[[], datetime] = (
            (lambda: datetime.now(UTC)) if now_fn is None else now_fn
        )

    def probe(self) -> WorkstationSnapshot:
        """Capture local `WorkstationSnapshot` from environment overrides and OS sockets."""
        env = os.environ if self._environ is None else self._environ
        captured_at = self._now_fn()
        hostname = _safe_hostname()
        ip_addresses = _resolve_ip_addresses(env, hostname)
        active_interfaces = _resolve_active_interfaces(env)
        wifi_ssid = env.get(ENV_SENSOR_WIFI_SSID, "").strip() or None
        display_count = _resolve_display_count(env)
        return WorkstationSnapshot(
            captured_at=captured_at,
            ip_addresses=ip_addresses,
            wifi_ssid=wifi_ssid,
            active_interfaces=active_interfaces,
            display_count=display_count,
            hostname=hostname,
        )


def _safe_hostname() -> str | None:
    """Return local workstation hostname or `None` on OS error."""
    try:
        name = socket.gethostname().strip()
        return name or None
    except OSError:
        return None


def _resolve_ip_addresses(
    env: Mapping[str, str],
    hostname: str | None,
) -> tuple[str, ...]:
    """Resolve local IPv4/IPv6 addresses from `WORKTRACK_SENSOR_IP_ADDRESSES` or `socket`."""
    override = env.get(ENV_SENSOR_IP_ADDRESSES, "").strip()
    if override:
        return tuple(part.strip() for part in override.split(",") if part.strip())
    if not hostname:
        return ()
    discovered: list[str] = []
    seen: set[str] = set()
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return ()
    for info in infos:
        sockaddr = info[4]
        ip_str = str(sockaddr[0]).strip()
        if ip_str and ip_str not in seen:
            seen.add(ip_str)
            discovered.append(ip_str)
    return tuple(discovered)


def _resolve_active_interfaces(env: Mapping[str, str]) -> tuple[str, ...]:
    """Resolve active network interface names from `WORKTRACK_SENSOR_INTERFACES` or `socket`."""
    override = env.get(ENV_SENSOR_INTERFACES, "").strip()
    if override:
        return tuple(part.strip() for part in override.split(",") if part.strip())
    if not hasattr(socket, "if_nameindex"):
        return ()
    try:
        return tuple(name for _, name in socket.if_nameindex() if name)
    except OSError:
        return ()


def _resolve_display_count(env: Mapping[str, str]) -> int | None:
    """Resolve optional attached display count from `WORKTRACK_SENSOR_DISPLAY_COUNT`."""
    raw = env.get(ENV_SENSOR_DISPLAY_COUNT, "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise SensorValidationError(
            f"{ENV_SENSOR_DISPLAY_COUNT} must be an integer, got '{raw}'."
        ) from exc
    if parsed < MIN_DISPLAY_COUNT:
        raise SensorValidationError(f"{ENV_SENSOR_DISPLAY_COUNT} must be >= 0.")
    return parsed


# endregion
