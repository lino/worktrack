"""Jira Cloud issue-to-`Ledger` graph upsert helpers (`Activity`, `Entity`, `Attribution`)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from worktrack.core import (
    ACTIVITY_KIND_TARGETED,
    ENTITY_KIND_COMPONENT,
    ENTITY_KIND_EPIC,
    ENTITY_KIND_PROJECT,
    MAX_ATTRIBUTION_WEIGHT,
    Activity,
    Entity,
    Ledger,
)
from worktrack.core.models import ENTITY_KEY_PATTERN, MAX_NAME_LENGTH, MAX_TAG_LENGTH

# region Constants
JIRA_SOURCE_SLUG: Final = "jira"
DONE_STATUS_CATEGORY: Final = "done"
NON_ENTITY_SLUG_CHARS: Final = re.compile(r"[^a-z0-9._:-]+")
MAX_ENTITY_KEY_LENGTH: Final = 128
# endregion


# region Models
@dataclass(slots=True)
class SyncCounters:
    """Mutable counters accumulated across synced Jira issues."""

    issues_fetched: int = 0
    activities_created: int = 0
    activities_updated: int = 0
    entities_created: int = 0
    attributions_upserted: int = 0


# endregion


# region Jira Source
def upsert_issue_graph(
    ledger: Ledger,
    raw_issue: Mapping[str, object],
    counters: SyncCounters,
) -> None:
    """Upsert Targeted `Activity`, `Entities`, and `Attribution` edges for one Jira issue."""
    raw_key = raw_issue.get("key")
    fields = raw_issue.get("fields")
    if not isinstance(raw_key, str) or not raw_key.strip() or not isinstance(fields, Mapping):
        return
    issue_key = raw_key.strip()
    activity = _upsert_issue_activity(ledger, issue_key, fields, counters)
    desired_edges: dict[str, float] = {}

    project_ent = _upsert_project_entity(ledger, fields, counters)
    if project_ent is not None:
        desired_edges[project_ent.id] = MAX_ATTRIBUTION_WEIGHT

    epic_ent = _upsert_epic_entity(ledger, fields, counters)
    if epic_ent is not None:
        desired_edges[epic_ent.id] = MAX_ATTRIBUTION_WEIGHT

    comp_entities = _upsert_component_entities(ledger, fields, counters)
    if comp_entities:
        comp_weight = MAX_ATTRIBUTION_WEIGHT / len(comp_entities)
        for comp in comp_entities:
            desired_edges[comp.id] = comp_weight

    _reconcile_activity_attributions(ledger, activity.id, desired_edges, counters)


def _upsert_issue_activity(
    ledger: Ledger,
    issue_key: str,
    fields: Mapping[str, object],
    counters: SyncCounters,
) -> Activity:
    """Create or update Targeted `Activity` keyed by `issue_key`."""
    raw_summary = fields.get("summary")
    summary = (
        raw_summary.strip()[:MAX_NAME_LENGTH]
        if isinstance(raw_summary, str) and raw_summary.strip()
        else issue_key
    )
    tags = _sanitize_jira_labels(fields.get("labels"))
    is_done = _is_issue_done(fields.get("status"))
    existing = ledger.find_activity_by_external_ref(issue_key, source=JIRA_SOURCE_SLUG)
    if existing is None:
        counters.activities_created += 1
        return ledger.create_activity(
            summary,
            kind=ACTIVITY_KIND_TARGETED,
            external_ref=issue_key,
            source=JIRA_SOURCE_SLUG,
            tags=tags,
            archived=is_done,
        )
    counters.activities_updated += 1
    return ledger.update_activity(existing.id, name=summary, archived=is_done, tags=tags)


def _upsert_project_entity(
    ledger: Ledger,
    fields: Mapping[str, object],
    counters: SyncCounters,
) -> Entity | None:
    """Upsert `project` `Entity` from Jira `fields.project`."""
    proj = fields.get("project")
    if not isinstance(proj, Mapping):
        return None
    raw_key = proj.get("key")
    if not isinstance(raw_key, str) or not ENTITY_KEY_PATTERN.match(raw_key.strip()):
        return None
    key = raw_key.strip()
    raw_name = proj.get("name")
    name = (
        raw_name.strip()[:MAX_NAME_LENGTH]
        if isinstance(raw_name, str) and raw_name.strip()
        else key
    )
    return _find_or_create_entity(
        ledger,
        ENTITY_KIND_PROJECT,
        key,
        name,
        {"source": JIRA_SOURCE_SLUG, "jira_id": str(proj.get("id", ""))},
        counters,
    )


def _upsert_epic_entity(
    ledger: Ledger,
    fields: Mapping[str, object],
    counters: SyncCounters,
) -> Entity | None:
    """Upsert `epic` `Entity` from Jira `fields.parent` (Epic) or `fields.epic`."""
    epic_info = _extract_parent_epic(fields.get("parent")) or _extract_legacy_epic(
        fields.get("epic")
    )
    if epic_info is None:
        return None
    epic_key, epic_name = epic_info
    return _find_or_create_entity(
        ledger,
        ENTITY_KIND_EPIC,
        epic_key,
        epic_name,
        {"source": JIRA_SOURCE_SLUG},
        counters,
    )


def _extract_parent_epic(parent: object) -> tuple[str, str] | None:
    """Extract `(key, name)` when `fields.parent` represents an Epic."""
    if not isinstance(parent, Mapping) or not isinstance(parent.get("key"), str):
        return None
    key = str(parent["key"]).strip()
    p_fields = parent.get("fields")
    if not isinstance(p_fields, Mapping) or not ENTITY_KEY_PATTERN.match(key):
        return None
    itype = p_fields.get("issuetype")
    type_name = str(itype.get("name", "")).lower() if isinstance(itype, Mapping) else "epic"
    if type_name and type_name != "epic":
        return None
    summary = p_fields.get("summary")
    name = (
        str(summary).strip()[:MAX_NAME_LENGTH]
        if isinstance(summary, str) and summary.strip()
        else key
    )
    return key, name


def _extract_legacy_epic(raw_epic: object) -> tuple[str, str] | None:
    """Extract `(key, name)` from legacy Jira `fields.epic` dictionary."""
    if not isinstance(raw_epic, Mapping) or not isinstance(raw_epic.get("key"), str):
        return None
    key = str(raw_epic["key"]).strip()
    if not ENTITY_KEY_PATTERN.match(key):
        return None
    raw_name = raw_epic.get("name") or raw_epic.get("summary") or key
    return key, str(raw_name).strip()[:MAX_NAME_LENGTH]


def _upsert_component_entities(
    ledger: Ledger,
    fields: Mapping[str, object],
    counters: SyncCounters,
) -> list[Entity]:
    """Upsert `component` `Entities` from Jira `fields.components`."""
    raw_comps = fields.get("components")
    if not isinstance(raw_comps, list):
        return []
    entities: list[Entity] = []
    seen_keys: set[str] = set()
    for comp in raw_comps:
        if not isinstance(comp, Mapping) or not isinstance(comp.get("name"), str):
            continue
        name = str(comp["name"]).strip()[:MAX_NAME_LENGTH]
        if not name:
            continue
        slug = _slugify_component_key(name, fallback_id=str(comp.get("id", "")))
        if slug in seen_keys:
            continue
        seen_keys.add(slug)
        entities.append(
            _find_or_create_entity(
                ledger,
                ENTITY_KIND_COMPONENT,
                slug,
                name,
                {"source": JIRA_SOURCE_SLUG, "jira_id": str(comp.get("id", ""))},
                counters,
            )
        )
    return entities


def _find_or_create_entity(
    ledger: Ledger,
    kind: str,
    key: str,
    name: str,
    metadata: Mapping[str, object],
    counters: SyncCounters,
) -> Entity:
    """Return existing `(kind, key)` `Entity` or create it in `ledger`."""
    existing = ledger.find_entity(kind, key)
    if existing is not None:
        return existing
    counters.entities_created += 1
    return ledger.create_entity(kind, key, name, metadata=metadata)


def _reconcile_activity_attributions(
    ledger: Ledger,
    activity_id: str,
    desired_edges: Mapping[str, float],
    counters: SyncCounters,
) -> None:
    """Upsert desired `Activity -> Entity` edges and prune removed Jira edges."""
    for existing_edge in ledger.list_attributions(source_activity_id=activity_id):
        if existing_edge.target_entity_id not in desired_edges:
            target = ledger.get_entity(existing_edge.target_entity_id)
            if target.metadata.get("source") == JIRA_SOURCE_SLUG:
                ledger.remove_attribution(existing_edge.id)
    for target_entity_id, weight in desired_edges.items():
        ledger.set_attribution(
            source_activity_id=activity_id,
            target_entity_id=target_entity_id,
            weight=weight,
        )
        counters.attributions_upserted += 1


def _slugify_component_key(name: str, *, fallback_id: str) -> str:
    """Normalize a Jira component display name into a valid `Entity.key`."""
    candidate = NON_ENTITY_SLUG_CHARS.sub("-", name.strip().lower()).strip("-._:")
    if candidate and ENTITY_KEY_PATTERN.match(candidate):
        return candidate[:MAX_ENTITY_KEY_LENGTH]
    fallback_slug = NON_ENTITY_SLUG_CHARS.sub("-", fallback_id.lower()).strip("-") or "item"
    return f"comp-{fallback_slug}"[:MAX_ENTITY_KEY_LENGTH]


def _sanitize_jira_labels(raw_labels: object) -> Sequence[str]:
    """Filter Jira issue labels into bounded non-empty Worktrack tags."""
    if not isinstance(raw_labels, list):
        return ()
    cleaned = [
        item.strip()
        for item in raw_labels
        if isinstance(item, str) and 0 < len(item.strip()) <= MAX_TAG_LENGTH
    ]
    return tuple(cleaned)


def _is_issue_done(raw_status: object) -> bool:
    """Return `True` when Jira `fields.status.statusCategory.key` equals `'done'`."""
    if not isinstance(raw_status, Mapping):
        return False
    category = raw_status.get("statusCategory")
    if not isinstance(category, Mapping):
        return False
    return str(category.get("key", "")).strip().lower() == DONE_STATUS_CATEGORY


# endregion
