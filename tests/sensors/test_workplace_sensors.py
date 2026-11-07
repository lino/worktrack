"""Behavioural tests for `WorkplaceSensorRule`, `WorkstationProbe`, and `WorkplaceSensorEngine`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from worktrack.collectors import CLUE_SOURCE_SENSOR, ClueStore
from worktrack.core import BUILTIN_WORKPLACE_FACET_KEY, Ledger
from worktrack.sensors import (
    SENSOR_CONTEXT_FACT_SOURCE,
    LocalWorkstationProbe,
    SensorValidationError,
    WorkplaceSensorEngine,
    WorkplaceSensorRule,
    WorkplaceSuggestion,
    WorkstationSnapshot,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
SAMPLE_CAPTURED_AT = datetime(2026, 11, 7, 8, 30, 0, tzinfo=UTC)
SAMPLE_SCOPE_DATE = "2026-11-07"
OFFICE_DISPLAY_COUNT = 2
MOBILE_DISPLAY_COUNT = 1
CONFIDENCE_OFFICE = 0.95
CONFIDENCE_HOME = 0.90
CONFIDENCE_MOBILE = 0.80
INVALID_CONFIDENCE = 1.25
# endregion


# region Fixtures
class _StaticWorkstationProbe:
    """Injectable `WorkstationProbe` returning a deterministic `WorkstationSnapshot`."""

    def __init__(self, snapshot: WorkstationSnapshot) -> None:
        """Initialize with a fixed `WorkstationSnapshot`."""
        self._snapshot = snapshot

    def probe(self) -> WorkstationSnapshot:
        """Return the configured `WorkstationSnapshot`."""
        return self._snapshot


def _build_standard_rules() -> tuple[WorkplaceSensorRule, ...]:
    """Build ordered office, home-office, and mobile-work sensor rules."""
    return (
        WorkplaceSensorRule(
            rule_id="corp_hq_docked",
            workplace="office",
            cidrs=("10.40.0.0/16", "2001:db8:c0aa::/48"),
            wifi_ssids=("Acme-HQ-5G", "Acme-Corp"),
            min_displays=OFFICE_DISPLAY_COUNT,
            confidence=CONFIDENCE_OFFICE,
            description="Docked at corporate headquarters Wi-Fi and subnet.",
        ),
        WorkplaceSensorRule(
            rule_id="home_fiber_vpn",
            workplace="home_office",
            cidrs=("192.168.178.0/24", "fd00:cafe:beef::/48"),
            wifi_ssids=("Home-Fiber",),
            vpn_interface_prefixes=("utun", "wg"),
            min_displays=OFFICE_DISPLAY_COUNT,
            confidence=CONFIDENCE_HOME,
            description="Home LAN with active WireGuard/utun VPN and external monitor.",
        ),
        WorkplaceSensorRule(
            rule_id="road_hotspot",
            workplace="mobile_work",
            wifi_ssids=("Lino-iPhone", "DB-WIFIonICE"),
            max_displays=MOBILE_DISPLAY_COUNT,
            vpn_interface_prefixes=("utun", "wg"),
            require_vpn=False,
            confidence=CONFIDENCE_MOBILE,
            description="Single laptop display on mobile hotspot without VPN.",
        ),
    )


# endregion


# region Sensor Rule & Probe Tests
def test_workplace_sensor_rule_matches_ipv4_ipv6_cidr_ssid_vpn_and_displays() -> None:
    """Verify `WorkplaceSensorRule` evaluates IPv4/IPv6 CIDR, SSID, VPN prefix, and displays."""
    rules = _build_standard_rules()
    office_rule, home_rule, mobile_rule = rules

    ipv6_home_snapshot = WorkstationSnapshot(
        captured_at=SAMPLE_CAPTURED_AT,
        ip_addresses=("fe80::1%en0", "fd00:cafe:beef::42"),
        wifi_ssid="Home-Fiber",
        active_interfaces=("lo0", "en0", "wg0"),
        display_count=OFFICE_DISPLAY_COUNT,
    )
    matched, signals = home_rule.evaluate(ipv6_home_snapshot)
    assert matched is True
    assert "cidr:fd00:cafe:beef::/48" in signals
    assert "wifi_ssid:Home-Fiber" in signals
    assert "vpn_interface:wg0" in signals
    assert f"displays:{OFFICE_DISPLAY_COUNT}" in signals

    office_matched, _ = office_rule.evaluate(ipv6_home_snapshot)
    assert office_matched is False

    mobile_snapshot = WorkstationSnapshot(
        captured_at=SAMPLE_CAPTURED_AT,
        ip_addresses=("172.20.10.2",),
        wifi_ssid="DB-WIFIonICE",
        active_interfaces=("lo0", "en0"),
        display_count=MOBILE_DISPLAY_COUNT,
    )
    mob_matched, mob_signals = mobile_rule.evaluate(mobile_snapshot)
    assert mob_matched is True
    assert "wifi_ssid:DB-WIFIonICE" in mob_signals
    assert "vpn_absent" in mob_signals


def test_workplace_sensor_rule_validation_rejects_invalid_inputs() -> None:
    """Verify `WorkplaceSensorRule` rejects bad CIDRs, unknown workplaces, and empty predicates."""
    with pytest.raises(SensorValidationError, match="CIDR"):
        WorkplaceSensorRule(
            rule_id="bad_cidr",
            workplace="office",
            cidrs=("not-a-subnet",),
        )
    with pytest.raises(SensorValidationError, match="workplace"):
        WorkplaceSensorRule(
            rule_id="bad_workplace",
            workplace="beach_cabana",
            wifi_ssids=("Beach-WiFi",),
        )
    with pytest.raises(SensorValidationError, match="confidence"):
        WorkplaceSensorRule(
            rule_id="bad_conf",
            workplace="office",
            wifi_ssids=("Corp",),
            confidence=INVALID_CONFIDENCE,
        )
    with pytest.raises(SensorValidationError, match="condition"):
        WorkplaceSensorRule(
            rule_id="empty_rule",
            workplace="office",
        )


def test_local_workstation_probe_captures_safe_local_snapshot() -> None:
    """Verify `LocalWorkstationProbe` honors environment overrides and makes zero external calls."""
    probe = LocalWorkstationProbe(
        environ={
            "WORKTRACK_SENSOR_WIFI_SSID": "Home-Fiber",
            "WORKTRACK_SENSOR_DISPLAY_COUNT": str(OFFICE_DISPLAY_COUNT),
            "WORKTRACK_SENSOR_IP_ADDRESSES": "192.168.178.25, fd00:cafe:beef::9",
            "WORKTRACK_SENSOR_INTERFACES": "lo0,en0,utun4",
        },
        now_fn=lambda: SAMPLE_CAPTURED_AT,
    )
    snapshot = probe.probe()
    assert snapshot.captured_at == SAMPLE_CAPTURED_AT
    assert snapshot.wifi_ssid == "Home-Fiber"
    assert snapshot.display_count == OFFICE_DISPLAY_COUNT
    assert "192.168.178.25" in snapshot.ip_addresses
    assert "utun4" in snapshot.active_interfaces


# endregion


# region Sensor Engine Tests
def test_workplace_sensor_engine_ordered_evaluation_emits_clue_and_asserts_context_fact(
    tmp_path: Path,
) -> None:
    """Verify `WorkplaceSensorEngine` evaluates rules in order, emits `Clue`, and asserts fact."""
    db_path = tmp_path / "sensors.sqlite3"
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    snapshot = WorkstationSnapshot(
        captured_at=SAMPLE_CAPTURED_AT,
        ip_addresses=("192.168.178.55",),
        wifi_ssid="Home-Fiber",
        active_interfaces=("lo0", "en0", "utun3"),
        display_count=OFFICE_DISPLAY_COUNT,
    )

    with Ledger(db_path, telemetry=telemetry) as ledger, ClueStore(db_path) as clue_store:
        engine = WorkplaceSensorEngine(
            rules=_build_standard_rules(),
            probe=_StaticWorkstationProbe(snapshot),
            clue_store=clue_store,
            ledger=ledger,
        )
        suggestion = engine.evaluate(assert_fact=True)
        assert isinstance(suggestion, WorkplaceSuggestion)
        assert suggestion.workplace == "home_office"
        assert suggestion.rule_id == "home_fiber_vpn"
        assert suggestion.scope_date == SAMPLE_SCOPE_DATE
        assert suggestion.clue is not None
        assert suggestion.clue.source == CLUE_SOURCE_SENSOR
        assert suggestion.clue.metadata["workplace"] == "home_office"
        assert suggestion.clue.metadata["rule_id"] == "home_fiber_vpn"
        assert suggestion.context_fact is not None
        assert suggestion.context_fact.value == "home_office"
        assert suggestion.context_fact.source == SENSOR_CONTEXT_FACT_SOURCE

        stored_fact = ledger.get_context_fact(
            BUILTIN_WORKPLACE_FACET_KEY,
            scope_date=SAMPLE_SCOPE_DATE,
        )
        assert stored_fact is not None
        assert stored_fact.value == "home_office"
        assert len(clue_store.list_clues(source=CLUE_SOURCE_SENSOR)) == 1

    exposition = telemetry.render_metrics_text()
    assert "worktrack_context_facts_asserted_total 1.0" in exposition


def test_workplace_sensor_engine_preserves_manual_fact_unless_overwrite_enabled(
    tmp_path: Path,
) -> None:
    """Verify manual `workplace` `ContextFact` is preserved unless `overwrite_manual=True`."""
    db_path = tmp_path / "manual_guard.sqlite3"
    snapshot = WorkstationSnapshot(
        captured_at=SAMPLE_CAPTURED_AT,
        ip_addresses=("192.168.178.55",),
        wifi_ssid="Home-Fiber",
        active_interfaces=("en0", "utun2"),
        display_count=OFFICE_DISPLAY_COUNT,
    )

    with Ledger(db_path) as ledger, ClueStore(db_path) as clue_store:
        ledger.assert_context_fact(
            BUILTIN_WORKPLACE_FACET_KEY,
            "office",
            scope_date=SAMPLE_SCOPE_DATE,
            source="manual",
        )
        engine = WorkplaceSensorEngine(
            rules=_build_standard_rules(),
            probe=_StaticWorkstationProbe(snapshot),
            clue_store=clue_store,
            ledger=ledger,
        )
        preserved = engine.evaluate(assert_fact=True, overwrite_manual=False)
        assert preserved is not None
        assert preserved.workplace == "home_office"
        assert preserved.clue is not None
        assert preserved.context_fact is not None
        assert preserved.context_fact.value == "office"
        assert preserved.context_fact.source == "manual"

        overwritten = engine.evaluate(assert_fact=True, overwrite_manual=True)
        assert overwritten is not None
        assert overwritten.context_fact is not None
        assert overwritten.context_fact.value == "home_office"
        assert overwritten.context_fact.source == SENSOR_CONTEXT_FACT_SOURCE


# endregion
