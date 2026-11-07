"""Topological DAG duration propagation for `EntityAttributionShare` queries."""

from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from worktrack.core.entities import list_attributions, list_entities
from worktrack.core.exceptions import AttributionCycleError, LedgerValidationError
from worktrack.core.models import (
    MICROSECONDS_PER_SECOND,
    Attribution,
    Entity,
    EntityAttributionShare,
    ensure_utc,
    format_utc_iso,
    parse_utc_iso,
    validate_lower_slug,
)

# region Constants
ZERO_SECONDS: Final = 0.0
# endregion


# region Attribution
def query_entity_attribution(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    target_kind: str | None = None,
    normalize: bool = False,
) -> list[EntityAttributionShare]:
    """Propagate interval durations across the `Activity -> Entity` DAG."""
    clean_kind = (
        None if target_kind is None else validate_lower_slug(target_kind, field_name="target_kind")
    )
    activity_seconds = _aggregate_activity_seconds(conn, since=since, until=until)
    if not activity_seconds:
        return []
    entities = {ent.id: ent for ent in list_entities(conn)}
    if not entities:
        return []
    edges = list_attributions(conn)
    node_seconds = _propagate_dag_seconds(
        activity_seconds=activity_seconds,
        entities=entities,
        edges=edges,
        normalize=normalize,
    )
    return _build_attribution_shares(
        entities=entities, node_seconds=node_seconds, target_kind=clean_kind
    )


def _aggregate_activity_seconds(
    conn: sqlite3.Connection,
    *,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, float]:
    """Sum closed `Interval` durations per `Activity`, clipping to `[since, until)`."""
    if since is None and until is None:
        rows = conn.execute(
            """
            SELECT activity_id, SUM(duration_us) AS total_us
            FROM intervals
            WHERE activity_id IS NOT NULL AND duration_us IS NOT NULL
            GROUP BY activity_id
            """
        ).fetchall()
        return {
            str(row["activity_id"]): int(row["total_us"]) / MICROSECONDS_PER_SECOND for row in rows
        }
    return _aggregate_clipped_activity_seconds(conn, since=since, until=until)


def _normalize_clip_bounds(
    since: datetime | None,
    until: datetime | None,
) -> tuple[datetime | None, datetime | None, str | None, str | None]:
    """Validate optional `[since, until)` bounds and format ISO predicates."""
    utc_since = ensure_utc(since, field_name="since") if since is not None else None
    utc_until = ensure_utc(until, field_name="until") if until is not None else None
    if utc_since is not None and utc_until is not None and utc_until <= utc_since:
        raise LedgerValidationError("until must be strictly greater than since.")
    since_iso = format_utc_iso(utc_since) if utc_since is not None else None
    until_iso = format_utc_iso(utc_until) if utc_until is not None else None
    return utc_since, utc_until, since_iso, until_iso


def _aggregate_clipped_activity_seconds(
    conn: sqlite3.Connection,
    *,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, float]:
    """Clip boundary-spanning closed `Intervals` to `[since, until)` and sum per activity."""
    utc_since, utc_until, since_iso, until_iso = _normalize_clip_bounds(since, until)
    clauses = ["activity_id IS NOT NULL", "ended_at IS NOT NULL"]
    params: list[object] = []
    if until_iso is not None:
        clauses.append("started_at < ?")
        params.append(until_iso)
    if since_iso is not None:
        clauses.append("ended_at > ?")
        params.append(since_iso)

    rows = conn.execute(
        f"""
        SELECT activity_id, started_at, ended_at, duration_us
        FROM intervals
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()
    totals: dict[str, float] = {}
    for row in rows:
        seconds = _clipped_row_seconds(row, utc_since, utc_until, since_iso, until_iso)
        if seconds > ZERO_SECONDS:
            act_id = str(row["activity_id"])
            totals[act_id] = totals.get(act_id, ZERO_SECONDS) + seconds
    return totals


def _clipped_row_seconds(
    row: sqlite3.Row,
    utc_since: datetime | None,
    utc_until: datetime | None,
    since_iso: str | None,
    until_iso: str | None,
) -> float:
    """Compute effective seconds for a closed `Interval` row inside `[since, until)`."""
    start_str = str(row["started_at"])
    end_str = str(row["ended_at"])
    needs_start_clip = since_iso is not None and start_str < since_iso
    needs_end_clip = until_iso is not None and end_str > until_iso
    if not needs_start_clip and not needs_end_clip:
        return int(row["duration_us"]) / MICROSECONDS_PER_SECOND
    eff_start = parse_utc_iso(start_str)
    eff_end = parse_utc_iso(end_str)
    if utc_since is not None and eff_start < utc_since:
        eff_start = utc_since
    if utc_until is not None and eff_end > utc_until:
        eff_end = utc_until
    return max(ZERO_SECONDS, (eff_end - eff_start).total_seconds())


def _propagate_dag_seconds(
    *,
    activity_seconds: Mapping[str, float],
    entities: Mapping[str, Entity],
    edges: list[Attribution],
    normalize: bool,
) -> dict[str, float]:
    """Propagate activity seconds through `Activity -> Entity -> ...` in topological order."""
    act_edges: dict[str, list[tuple[str, float]]] = {}
    ent_edges: dict[str, list[tuple[str, float]]] = {}
    indegree: dict[str, int] = dict.fromkeys(entities, 0)

    for edge in edges:
        if edge.source_activity_id is not None:
            act_edges.setdefault(edge.source_activity_id, []).append(
                (edge.target_entity_id, edge.weight)
            )
        elif edge.source_entity_id is not None:
            ent_edges.setdefault(edge.source_entity_id, []).append(
                (edge.target_entity_id, edge.weight)
            )
            indegree[edge.target_entity_id] = indegree.get(edge.target_entity_id, 0) + 1

    node_seconds: dict[str, float] = dict.fromkeys(entities, ZERO_SECONDS)
    for act_id, secs in activity_seconds.items():
        for target_id, eff_w in _effective_edges(
            act_edges.get(act_id, ()), entities, normalize=normalize
        ):
            node_seconds[target_id] += secs * eff_w

    for current_id in _topological_order(entities, ent_edges, indegree):
        current_secs = node_seconds[current_id]
        if current_secs <= ZERO_SECONDS:
            continue
        for target_id, eff_w in _effective_edges(
            ent_edges.get(current_id, ()), entities, normalize=normalize
        ):
            node_seconds[target_id] += current_secs * eff_w
    return node_seconds


def _effective_edges(
    outgoing: list[tuple[str, float]] | tuple[tuple[str, float], ...],
    entities: Mapping[str, Entity],
    *,
    normalize: bool,
) -> list[tuple[str, float]]:
    """Compute effective outgoing edge weights (optionally normalized per target kind)."""
    if not outgoing or not normalize:
        return list(outgoing)
    sums_by_kind: dict[str, float] = {}
    for target_id, weight in outgoing:
        kind = entities[target_id].kind
        sums_by_kind[kind] = sums_by_kind.get(kind, ZERO_SECONDS) + weight
    return [
        (target_id, weight / sums_by_kind[entities[target_id].kind])
        for target_id, weight in outgoing
        if sums_by_kind[entities[target_id].kind] > ZERO_SECONDS
    ]


def _topological_order(
    entities: Mapping[str, Entity],
    ent_edges: Mapping[str, list[tuple[str, float]]],
    indegree: dict[str, int],
) -> list[str]:
    """Compute deterministic Kahn topological ordering across `Entity` nodes."""
    queue: deque[str] = deque(sorted(eid for eid, deg in indegree.items() if deg == 0))
    ordered: list[str] = []
    while queue:
        current = queue.popleft()
        ordered.append(current)
        for target_id, _ in ent_edges.get(current, ()):
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                queue.append(target_id)
    if len(ordered) != len(entities):
        raise AttributionCycleError("Cycle detected in persisted Entity Attribution graph.")
    return ordered


def _build_attribution_shares(
    *,
    entities: Mapping[str, Entity],
    node_seconds: Mapping[str, float],
    target_kind: str | None,
) -> list[EntityAttributionShare]:
    """Filter, compute per-kind relative share, and sort `EntityAttributionShare` rows."""
    candidates = [
        (entities[eid], secs)
        for eid, secs in node_seconds.items()
        if secs > ZERO_SECONDS and (target_kind is None or entities[eid].kind == target_kind)
    ]
    kind_totals: dict[str, float] = {}
    for entity, secs in candidates:
        kind_totals[entity.kind] = kind_totals.get(entity.kind, ZERO_SECONDS) + secs

    shares = [
        EntityAttributionShare(
            entity=entity,
            attributed_seconds=secs,
            share_of_kind=secs / kind_totals[entity.kind]
            if kind_totals[entity.kind] > ZERO_SECONDS
            else ZERO_SECONDS,
        )
        for entity, secs in candidates
    ]
    shares.sort(key=lambda item: (-item.attributed_seconds, item.entity.kind, item.entity.key))
    return shares


# endregion
