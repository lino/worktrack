"""`WorkplaceSensorEngine` evaluating ordered rules to emit `Clues` and `ContextFacts`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date

from worktrack.collectors.models import CLUE_SOURCE_SENSOR, Clue
from worktrack.collectors.store import ClueStore
from worktrack.core import (
    BUILTIN_WORKPLACE_FACET_KEY,
    DEFAULT_CONTEXT_FACT_SOURCE,
    ContextFact,
    Ledger,
)
from worktrack.core.models import normalize_scope_date
from worktrack.sensors.models import (
    SENSOR_CONTEXT_FACT_SOURCE,
    SensorValidationError,
    WorkplaceSensorRule,
    WorkplaceSuggestion,
    WorkstationSnapshot,
)
from worktrack.sensors.probe import LocalWorkstationProbe, WorkstationProbe


# region Sensor Engine
class WorkplaceSensorEngine:
    """Engine evaluating ordered `WorkplaceSensorRule`s against a `WorkstationProbe`."""

    def __init__(
        self,
        *,
        rules: Sequence[WorkplaceSensorRule],
        probe: WorkstationProbe | None = None,
        clue_store: ClueStore | None = None,
        ledger: Ledger | None = None,
        auto_assert_fact: bool = False,
    ) -> None:
        """Initialize `WorkplaceSensorEngine` with ordered rules and optional stores."""
        self._rules = tuple(rules)
        self._probe: WorkstationProbe = LocalWorkstationProbe() if probe is None else probe
        self._clue_store = clue_store
        self._ledger = ledger
        self._auto_assert_fact = auto_assert_fact

    @property
    def rules(self) -> tuple[WorkplaceSensorRule, ...]:
        """Return ordered tuple of configured `WorkplaceSensorRule` items."""
        return self._rules

    def evaluate(
        self,
        *,
        snapshot: WorkstationSnapshot | None = None,
        scope_date: str | date | None = None,
        assert_fact: bool | None = None,
        overwrite_manual: bool = False,
        clue_store: ClueStore | None = None,
        ledger: Ledger | None = None,
    ) -> WorkplaceSuggestion | None:
        """Probe workstation, evaluate ordered rules, and optionally persist `Clue`/fact."""
        active_snapshot = self._probe.probe() if snapshot is None else snapshot
        resolved_date = _resolve_scope_date(scope_date, active_snapshot)
        matched_rule, matched_signals = self._find_first_matching_rule(active_snapshot)
        if matched_rule is None:
            return None

        active_clue_store = self._clue_store if clue_store is None else clue_store
        active_ledger = self._ledger if ledger is None else ledger
        should_assert = self._auto_assert_fact if assert_fact is None else assert_fact

        persisted_clue = _emit_sensor_clue(
            clue_store=active_clue_store,
            rule=matched_rule,
            scope_date=resolved_date,
            matched_signals=matched_signals,
            snapshot=active_snapshot,
        )
        persisted_fact = _maybe_assert_workplace_fact(
            ledger=active_ledger,
            rule=matched_rule,
            scope_date=resolved_date,
            should_assert=should_assert,
            overwrite_manual=overwrite_manual,
        )
        return WorkplaceSuggestion(
            scope_date=resolved_date,
            workplace=matched_rule.workplace,
            rule_id=matched_rule.rule_id,
            confidence=matched_rule.confidence,
            matched_signals=matched_signals,
            snapshot=active_snapshot,
            clue=persisted_clue,
            context_fact=persisted_fact,
        )

    def _find_first_matching_rule(
        self,
        snapshot: WorkstationSnapshot,
    ) -> tuple[WorkplaceSensorRule | None, tuple[str, ...]]:
        """Evaluate rules in order and return the first matching rule and its signals."""
        for rule in self._rules:
            matched, signals = rule.evaluate(snapshot)
            if matched:
                return rule, signals
        return None, ()


def _resolve_scope_date(
    explicit_date: str | date | None,
    snapshot: WorkstationSnapshot,
) -> str:
    """Resolve `YYYY-MM-DD` scope date from explicit argument or `snapshot.captured_at`."""
    if explicit_date is not None:
        try:
            return normalize_scope_date(explicit_date)
        except Exception as exc:
            raise SensorValidationError(str(exc)) from exc
    return snapshot.captured_at.astimezone(UTC).date().isoformat()


def _emit_sensor_clue(
    *,
    clue_store: ClueStore | None,
    rule: WorkplaceSensorRule,
    scope_date: str,
    matched_signals: tuple[str, ...],
    snapshot: WorkstationSnapshot,
) -> Clue | None:
    """Persist a provenance-tagged `Clue` (`source='sensor'`) in `clue_store` if attached."""
    if clue_store is None:
        return None
    joined_signals = ", ".join(matched_signals)
    summary = f"Inferred workplace '{rule.workplace}' via rule '{rule.rule_id}' ({joined_signals})"
    external_key = f"workplace:{scope_date}:{rule.rule_id}"
    metadata: dict[str, object] = {
        "workplace": rule.workplace,
        "rule_id": rule.rule_id,
        "scope_date": scope_date,
        "matched_signals": list(matched_signals),
        "wifi_ssid": snapshot.wifi_ssid,
        "display_count": snapshot.display_count,
        "ip_addresses": list(snapshot.ip_addresses),
        "active_interfaces": list(snapshot.active_interfaces),
    }
    return clue_store.upsert_clue(
        source=CLUE_SOURCE_SENSOR,
        external_key=external_key,
        occurred_at=snapshot.captured_at,
        summary=summary,
        inferred_activity_key=None,
        confidence=rule.confidence,
        metadata=metadata,
    )


def _maybe_assert_workplace_fact(
    *,
    ledger: Ledger | None,
    rule: WorkplaceSensorRule,
    scope_date: str,
    should_assert: bool,
    overwrite_manual: bool,
) -> ContextFact | None:
    """Assert daily `workplace` `ContextFact` in `ledger` when enabled."""
    if ledger is None or not should_assert:
        return None
    existing = ledger.get_context_fact(BUILTIN_WORKPLACE_FACET_KEY, scope_date=scope_date)
    if (
        existing is not None
        and existing.source == DEFAULT_CONTEXT_FACT_SOURCE
        and not overwrite_manual
    ):
        return existing
    return ledger.assert_context_fact(
        BUILTIN_WORKPLACE_FACET_KEY,
        rule.workplace,
        scope_date=scope_date,
        source=SENSOR_CONTEXT_FACT_SOURCE,
    )


# endregion
