"""Domain models, constants, exceptions, and rule evaluators for `worktrack.sensors`."""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from worktrack.collectors.models import Clue
from worktrack.core.exceptions import LedgerError
from worktrack.core.models import (
    BUILTIN_WORKPLACE_VALUES,
    MAX_IDENTIFIER_LENGTH,
    ContextFact,
    ensure_utc,
    format_utc_iso,
)

# region Constants
SENSOR_CONTEXT_FACT_SOURCE: Final = "sensor"
DEFAULT_SENSOR_CONFIDENCE: Final = 0.90
MIN_SENSOR_CONFIDENCE: Final = 0.0
MAX_SENSOR_CONFIDENCE: Final = 1.0
MIN_DISPLAY_COUNT: Final = 0

ENV_SENSOR_WIFI_SSID: Final = "WORKTRACK_SENSOR_WIFI_SSID"
ENV_SENSOR_DISPLAY_COUNT: Final = "WORKTRACK_SENSOR_DISPLAY_COUNT"
ENV_SENSOR_IP_ADDRESSES: Final = "WORKTRACK_SENSOR_IP_ADDRESSES"
ENV_SENSOR_INTERFACES: Final = "WORKTRACK_SENSOR_INTERFACES"

DEFAULT_VPN_INTERFACE_PREFIXES: Final[tuple[str, ...]] = (
    "utun",
    "wg",
    "tun",
    "tap",
    "tailscale",
    "ppp",
)

type IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
type IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
# endregion


# region Exceptions
class SensorError(LedgerError):
    """Base exception for local workplace sensor probing and rule evaluation."""


class SensorValidationError(SensorError):
    """Raised when a `WorkplaceSensorRule` or `WorkstationSnapshot` is invalid."""


# endregion


# region Models
@dataclass(frozen=True, slots=True)
class WorkstationSnapshot:
    """Point-in-time local workstation network and display topology snapshot."""

    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ip_addresses: tuple[str, ...] = ()
    wifi_ssid: str | None = None
    active_interfaces: tuple[str, ...] = ()
    display_count: int | None = None
    hostname: str | None = None

    def __post_init__(self) -> None:
        """Normalize `captured_at` to UTC and sequences to tuples."""
        try:
            utc_ts = ensure_utc(self.captured_at, field_name="captured_at")
        except LedgerError as exc:
            raise SensorValidationError(str(exc)) from exc
        object.__setattr__(self, "captured_at", utc_ts)
        object.__setattr__(
            self,
            "ip_addresses",
            tuple(str(ip).strip() for ip in self.ip_addresses if str(ip).strip()),
        )
        object.__setattr__(
            self,
            "active_interfaces",
            tuple(str(iface).strip() for iface in self.active_interfaces if str(iface).strip()),
        )
        if self.wifi_ssid is not None:
            cleaned_ssid = self.wifi_ssid.strip()
            object.__setattr__(self, "wifi_ssid", cleaned_ssid or None)
        if self.display_count is not None and (
            isinstance(self.display_count, bool) or self.display_count < MIN_DISPLAY_COUNT
        ):
            raise SensorValidationError("display_count must be an integer >= 0.")

    def to_dict(self) -> dict[str, object]:
        """Serialize `WorkstationSnapshot` to a JSON-compatible dictionary."""
        return {
            "captured_at": format_utc_iso(self.captured_at),
            "ip_addresses": list(self.ip_addresses),
            "wifi_ssid": self.wifi_ssid,
            "active_interfaces": list(self.active_interfaces),
            "display_count": self.display_count,
            "hostname": self.hostname,
        }


@dataclass(frozen=True, slots=True)
class WorkplaceSensorRule:
    """User-configured rule mapping local workstation signals to a `workplace` value."""

    rule_id: str
    workplace: str
    cidrs: tuple[str, ...] = ()
    wifi_ssids: tuple[str, ...] = ()
    vpn_interface_prefixes: tuple[str, ...] = ()
    display_count: int | None = None
    min_displays: int | None = None
    max_displays: int | None = None
    require_vpn: bool | None = None
    confidence: float = DEFAULT_SENSOR_CONFIDENCE
    description: str = ""

    def __post_init__(self) -> None:
        """Validate rule identifier, workplace, confidence, CIDRs, and predicates."""
        _validate_and_normalize_rule(self)

    def evaluate(self, snapshot: WorkstationSnapshot) -> tuple[bool, tuple[str, ...]]:
        """Evaluate rule against `snapshot`, returning `(matched, matched_signals)`."""
        return _evaluate_rule_against_snapshot(self, snapshot)

    def matches(self, snapshot: WorkstationSnapshot) -> bool:
        """Return `True` when all configured predicates on this rule match `snapshot`."""
        matched, _ = self.evaluate(snapshot)
        return matched


@dataclass(frozen=True, slots=True)
class WorkplaceSuggestion:
    """Evaluated workplace inference with rule provenance, `Clue`, and `ContextFact`."""

    scope_date: str
    workplace: str
    rule_id: str
    confidence: float
    matched_signals: tuple[str, ...]
    snapshot: WorkstationSnapshot
    clue: Clue | None = None
    context_fact: ContextFact | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize `WorkplaceSuggestion` to a JSON-compatible dictionary."""
        return {
            "scope_date": self.scope_date,
            "workplace": self.workplace,
            "rule_id": self.rule_id,
            "confidence": self.confidence,
            "matched_signals": list(self.matched_signals),
            "snapshot": self.snapshot.to_dict(),
            "clue_id": None if self.clue is None else self.clue.id,
            "context_fact_id": None if self.context_fact is None else self.context_fact.id,
        }


# endregion


# region Sensor Rules
def _validate_and_normalize_rule(rule: WorkplaceSensorRule) -> None:
    """Validate and normalize fields on a `WorkplaceSensorRule`."""
    clean_id = _validate_rule_id_and_workplace(rule.rule_id, rule.workplace, rule.confidence)
    norm_cidrs = _parse_and_normalize_cidrs(rule.cidrs)
    norm_ssids = tuple(s.strip() for s in rule.wifi_ssids if s.strip())
    norm_vpn = tuple(p.strip() for p in rule.vpn_interface_prefixes if p.strip())
    _validate_display_bounds(rule.display_count, rule.min_displays, rule.max_displays)

    has_condition = any(
        (
            norm_cidrs,
            norm_ssids,
            norm_vpn,
            rule.require_vpn is not None,
            rule.display_count is not None,
            rule.min_displays is not None,
            rule.max_displays is not None,
        )
    )
    if not has_condition:
        raise SensorValidationError(
            f"WorkplaceSensorRule '{clean_id}' must specify at least one matching condition."
        )

    object.__setattr__(rule, "rule_id", clean_id)
    object.__setattr__(rule, "workplace", rule.workplace.strip())
    object.__setattr__(rule, "cidrs", norm_cidrs)
    object.__setattr__(rule, "wifi_ssids", norm_ssids)
    object.__setattr__(rule, "vpn_interface_prefixes", norm_vpn)
    object.__setattr__(rule, "confidence", float(rule.confidence))


def _validate_rule_id_and_workplace(
    rule_id: str,
    workplace: str,
    confidence: float,
) -> str:
    """Validate `rule_id`, `workplace` enum membership, and `confidence` bounds."""
    clean_id = rule_id.strip()
    if not clean_id or len(clean_id) > MAX_IDENTIFIER_LENGTH:
        raise SensorValidationError(f"rule_id must be 1..{MAX_IDENTIFIER_LENGTH} characters.")
    if workplace.strip() not in BUILTIN_WORKPLACE_VALUES:
        raise SensorValidationError(
            f"workplace must be one of {BUILTIN_WORKPLACE_VALUES}, got {workplace!r}."
        )
    if (
        isinstance(confidence, bool)
        or not math.isfinite(confidence)
        or not (MIN_SENSOR_CONFIDENCE <= float(confidence) <= MAX_SENSOR_CONFIDENCE)
    ):
        raise SensorValidationError("Sensor rule confidence must be in [0.0, 1.0].")
    return clean_id


def _parse_and_normalize_cidrs(raw_cidrs: Sequence[str]) -> tuple[str, ...]:
    """Validate IPv4 and IPv6 CIDR strings via stdlib `ipaddress.ip_network`."""
    normalized: list[str] = []
    for raw in raw_cidrs:
        cleaned = str(raw).strip()
        if not cleaned:
            continue
        try:
            network = ipaddress.ip_network(cleaned, strict=False)
        except ValueError as exc:
            raise SensorValidationError(f"Invalid CIDR subnet '{raw}': {exc}") from exc
        normalized.append(str(network))
    return tuple(normalized)


def _validate_display_bounds(
    exact: int | None,
    minimum: int | None,
    maximum: int | None,
) -> None:
    """Validate optional display count predicates."""
    for name, val in (
        ("display_count", exact),
        ("min_displays", minimum),
        ("max_displays", maximum),
    ):
        if val is not None and (isinstance(val, bool) or val < MIN_DISPLAY_COUNT):
            raise SensorValidationError(f"{name} must be an integer >= 0.")
    if minimum is not None and maximum is not None and maximum < minimum:
        raise SensorValidationError("max_displays must be >= min_displays.")


def _evaluate_rule_against_snapshot(
    rule: WorkplaceSensorRule,
    snapshot: WorkstationSnapshot,
) -> tuple[bool, tuple[str, ...]]:
    """Evaluate all configured predicates on `rule` against `snapshot`."""
    signals: list[str] = []

    if rule.cidrs:
        matched_cidr = _match_cidr_networks(rule.cidrs, snapshot.ip_addresses)
        if matched_cidr is None:
            return False, ()
        signals.append(f"cidr:{matched_cidr}")

    if rule.wifi_ssids:
        if snapshot.wifi_ssid is None or snapshot.wifi_ssid not in rule.wifi_ssids:
            return False, ()
        signals.append(f"wifi_ssid:{snapshot.wifi_ssid}")

    vpn_ok, vpn_signal = _match_vpn_condition(rule, snapshot.active_interfaces)
    if not vpn_ok:
        return False, ()
    if vpn_signal is not None:
        signals.append(vpn_signal)

    disp_ok, disp_signal = _match_display_condition(rule, snapshot.display_count)
    if not disp_ok:
        return False, ()
    if disp_signal is not None:
        signals.append(disp_signal)

    return True, tuple(signals)


def _match_cidr_networks(
    rule_cidrs: Sequence[str],
    snapshot_ips: Sequence[str],
) -> str | None:
    """Return the first CIDR in `rule_cidrs` containing any IP in `snapshot_ips`."""
    parsed_ips = _parse_snapshot_ips(snapshot_ips)
    if not parsed_ips:
        return None
    for cidr_str in rule_cidrs:
        network: IpNetwork = ipaddress.ip_network(cidr_str, strict=False)
        for ip_obj in parsed_ips:
            if ip_obj.version == network.version and ip_obj in network:
                return str(network)
    return None


def _parse_snapshot_ips(snapshot_ips: Sequence[str]) -> list[IpAddress]:
    """Parse valid IPv4/IPv6 addresses from `snapshot_ips` (stripping `%zone` suffixes)."""
    parsed: list[IpAddress] = []
    for raw_ip in snapshot_ips:
        clean_ip = raw_ip.split("%", 1)[0].strip()
        if not clean_ip:
            continue
        try:
            parsed.append(ipaddress.ip_address(clean_ip))
        except ValueError:
            continue
    return parsed


def _match_vpn_condition(
    rule: WorkplaceSensorRule,
    active_interfaces: Sequence[str],
) -> tuple[bool, str | None]:
    """Evaluate active VPN interface prefix predicate."""
    prefixes = rule.vpn_interface_prefixes
    if not prefixes and rule.require_vpn is None:
        return True, None
    effective_prefixes = prefixes if prefixes else DEFAULT_VPN_INTERFACE_PREFIXES
    must_have_vpn = True if rule.require_vpn is None else rule.require_vpn
    matched_iface = next(
        (
            iface
            for iface in active_interfaces
            if any(iface.startswith(prefix) for prefix in effective_prefixes)
        ),
        None,
    )
    if must_have_vpn:
        if matched_iface is None:
            return False, None
        return True, f"vpn_interface:{matched_iface}"
    if matched_iface is not None:
        return False, None
    return True, "vpn_absent"


def _match_display_condition(
    rule: WorkplaceSensorRule,
    display_count: int | None,
) -> tuple[bool, str | None]:
    """Evaluate exact, minimum, and maximum attached display count predicates."""
    has_disp_rule = any(
        (
            rule.display_count is not None,
            rule.min_displays is not None,
            rule.max_displays is not None,
        )
    )
    if not has_disp_rule:
        return True, None
    if display_count is None:
        return False, None
    checks = (
        rule.display_count is None or display_count == rule.display_count,
        rule.min_displays is None or display_count >= rule.min_displays,
        rule.max_displays is None or display_count <= rule.max_displays,
    )
    if not all(checks):
        return False, None
    return True, f"displays:{display_count}"


# endregion
