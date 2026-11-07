"""Canonical models, constants, and validators for Ganglia-style `Node` <-> `Hub` sync."""

from __future__ import annotations

import dataclasses
import math
import os
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final

from worktrack.core.models import (
    ENTITY_KEY_PATTERN,
    ensure_utc,
    format_utc_iso,
    validate_attribution_weight,
    validate_entity_key,
    validate_lower_slug,
    validate_non_empty_text,
)
from worktrack.hub_sync.exceptions import HubConfigurationError, HubRollupValidationError
from worktrack.outbox.models import FORBIDDEN_PAYLOAD_KEYS, OutboxItem
from worktrack.secrets import DEFAULT_TOKEN_ACCOUNT

# region Constants
HUB_SINK_SLUG: Final = "hub"
HUB_PROVIDER_SLUG: Final = "hub"
HUB_CATALOG_SOURCE_SLUG: Final = "hub"

ENV_HUB_BASE_URL: Final = "WORKTRACK_HUB_BASE_URL"
ENV_HUB_NODE_ID: Final = "WORKTRACK_HUB_NODE_ID"
ENV_HUB_HTTP_TIMEOUT_SECONDS: Final = "WORKTRACK_HUB_HTTP_TIMEOUT_SECONDS"

DEFAULT_NODE_ID: Final = "local-node"
DEFAULT_HUB_HTTP_TIMEOUT_SECONDS: Final = 15.0
MIN_POSITIVE_TIMEOUT_SECONDS: Final = 0.0
MIN_POSITIVE_DURATION_SECONDS: Final = 1
DEFAULT_CATALOG_VERSION: Final = 1
SHA256_HEX_PATTERN: Final = re.compile(r"^[0-9a-fA-F]{64}$")

DEFAULT_ROLLUP_INGEST_PATH: Final = "/api/v1/hub/rollups"
DEFAULT_CATALOG_PULL_PATH: Final = "/api/v1/hub/catalog"

TRACE_ID_HEADER_NAME: Final = "X-Fritz-Trace-Id"
IDEMPOTENCY_HEADER_NAME: Final = "Idempotency-Key"
NODE_ID_HEADER_NAME: Final = "X-Worktrack-Node-Id"
PAYLOAD_SHA256_HEADER_NAME: Final = "X-Worktrack-Payload-Sha256"

HTTPS_SCHEME: Final = "https"
HTTP_SCHEME: Final = "http"
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

_EXTRA_HUB_FORBIDDEN_KEYS: Final[tuple[str, ...]] = (
    "context_facts",
    "raw_context_facts",
    "access_token",
    "refresh_token",
    "client_secret",
    "password",
    "token",
    "authorization",
    "secret",
)
HUB_FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = FORBIDDEN_PAYLOAD_KEYS.union(
    _EXTRA_HUB_FORBIDDEN_KEYS
)
# endregion


# region Validation
def validate_hub_url(url: str) -> str:
    """Validate and normalize a Hub base URL (`https://` or loopback `http://`)."""
    cleaned = url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(cleaned)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme == HTTPS_SCHEME and host) or (
        parsed.scheme == HTTP_SCHEME and host in LOOPBACK_HOSTS
    ):
        return cleaned
    raise HubConfigurationError("Hub base URL must use 'https://' or loopback 'http://127.0.0.1'.")


def validate_node_id(node_id: str) -> str:
    """Validate a Worktrack `Node` identifier against `ENTITY_KEY_PATTERN`."""
    stripped = node_id.strip()
    if not ENTITY_KEY_PATTERN.match(stripped):
        raise HubConfigurationError(
            f"Hub node_id '{node_id}' must match {ENTITY_KEY_PATTERN.pattern}."
        )
    return stripped


def validate_sha256_digest(digest: str, *, field_name: str = "payload_sha256") -> str:
    """Validate a 64-character hexadecimal SHA-256 digest."""
    stripped = digest.strip().lower()
    if not SHA256_HEX_PATTERN.match(stripped):
        raise HubRollupValidationError(
            f"{field_name} must be a 64-character hexadecimal SHA-256 digest."
        )
    return stripped


def validate_ganglia_rollup_payload(payload: Mapping[str, object]) -> None:
    """Reject any outbound or inbound payload containing raw `Intervals`, `Clues`, or secrets."""
    if not isinstance(payload, Mapping) or not payload:
        raise HubRollupValidationError("Ganglia rollup payload must be a non-empty mapping.")
    violating = _collect_forbidden_keys(payload)
    if violating:
        joined = ", ".join(sorted(violating))
        raise HubRollupValidationError(
            f"Anti-surveillance violation: forbidden key(s) in Hub rollup payload: {joined}."
        )
    raw_dur = payload.get("rounded_duration_seconds", payload.get("time_spent_seconds"))
    if raw_dur is not None and (
        isinstance(raw_dur, bool)
        or not isinstance(raw_dur, int)
        or raw_dur < MIN_POSITIVE_DURATION_SECONDS
    ):
        raise HubRollupValidationError("Aggregate rollup duration must be an integer >= 1 second.")


def _collect_forbidden_keys(value: object) -> set[str]:
    """Recursively collect forbidden raw/secret keys from nested mappings and sequences."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key_str = str(raw_key)
            if key_str.strip().lower() in HUB_FORBIDDEN_PAYLOAD_KEYS:
                found.add(key_str)
            found.update(_collect_forbidden_keys(child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found.update(_collect_forbidden_keys(item))
    return found


def _freeze_entity_shares(shares: Mapping[str, float] | None) -> Mapping[str, float]:
    """Validate and freeze optional `Entity` attribution share mapping."""
    if shares is None:
        return MappingProxyType({})
    cleaned: dict[str, float] = {}
    for raw_key, raw_val in shares.items():
        key = raw_key.strip()
        if not key:
            raise HubRollupValidationError("entity_shares keys must be non-empty strings.")
        cleaned[key] = validate_attribution_weight(float(raw_val))
    return MappingProxyType(cleaned)


# endregion


# region Models
@dataclass(frozen=True, slots=True)
class HubEndpointConfig:
    """Configuration for Ganglia-style `Node` <-> `Hub` rollup push and catalog pull."""

    base_url: str | None = None
    node_id: str | None = None
    provider_name: str = HUB_PROVIDER_SLUG
    account: str = DEFAULT_TOKEN_ACCOUNT
    timeout_seconds: float | None = None
    rollup_ingest_path: str = DEFAULT_ROLLUP_INGEST_PATH
    catalog_pull_path: str = DEFAULT_CATALOG_PULL_PATH

    def resolve_base_url(self, environ: Mapping[str, str] | None = None) -> str:
        """Resolve and validate Hub base URL from explicit config or `WORKTRACK_HUB_BASE_URL`."""
        env = os.environ if environ is None else environ
        candidate = (self.base_url or env.get(ENV_HUB_BASE_URL, "")).strip()
        if not candidate:
            raise HubConfigurationError(f"Provide base_url or set {ENV_HUB_BASE_URL}.")
        return validate_hub_url(candidate)

    def resolve_node_id(self, environ: Mapping[str, str] | None = None) -> str:
        """Resolve and validate logical `node_id` from config, env, or `DEFAULT_NODE_ID`."""
        env = os.environ if environ is None else environ
        return validate_node_id(self.node_id or env.get(ENV_HUB_NODE_ID, "") or DEFAULT_NODE_ID)

    def resolve_timeout(self, environ: Mapping[str, str] | None = None) -> float:
        """Resolve finite positive HTTP timeout in seconds."""
        env = os.environ if environ is None else environ
        raw = env.get(ENV_HUB_HTTP_TIMEOUT_SECONDS, "").strip()
        timeout = (
            self.timeout_seconds
            if self.timeout_seconds is not None
            else (float(raw) if raw else DEFAULT_HUB_HTTP_TIMEOUT_SECONDS)
        )
        if not math.isfinite(timeout) or timeout <= MIN_POSITIVE_TIMEOUT_SECONDS:
            raise HubConfigurationError("Hub HTTP timeout_seconds must be finite and > 0.")
        return timeout

    def build_url(self, path: str, *, environ: Mapping[str, str] | None = None) -> str:
        """Join relative endpoint `path` onto resolved Hub `base_url`."""
        rel = path if path.startswith("/") else f"/{path}"
        return f"{self.resolve_base_url(environ=environ)}{rel}"


@dataclass(frozen=True, slots=True)
class HubHttpResponse:
    """Decoded HTTP status, JSON payload, and response headers from the Hub."""

    status_code: int
    payload: Mapping[str, object]
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GangliaRollupEnvelope:
    """Sanitized Ganglia-style aggregate `Rollup` packet transmitted from `Node` to `Hub`."""

    node_id: str
    outbox_item_id: str
    idempotency_key: str
    target_ref: str
    window_start: datetime
    window_end: datetime
    rounded_duration_seconds: int
    summary: str
    cadence: str
    payload_preview: Mapping[str, object]
    payload_sha256: str
    entity_shares: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate envelope fields and freeze nested mappings."""
        validate_node_id(self.node_id)
        validate_sha256_digest(self.payload_sha256)
        validate_ganglia_rollup_payload(self.payload_preview)
        object.__setattr__(self, "payload_preview", MappingProxyType(dict(self.payload_preview)))
        object.__setattr__(self, "entity_shares", _freeze_entity_shares(self.entity_shares))

    @classmethod
    def from_outbox_item(
        cls,
        item: OutboxItem,
        *,
        node_id: str,
        entity_shares: Mapping[str, float] | None = None,
    ) -> GangliaRollupEnvelope:
        """Construct a `GangliaRollupEnvelope` from an approved `OutboxItem`."""
        return cls(
            node_id=validate_node_id(node_id),
            outbox_item_id=item.id,
            idempotency_key=item.idempotency_key,
            target_ref=item.target_ref,
            window_start=ensure_utc(item.window_start, field_name="window_start"),
            window_end=ensure_utc(item.window_end, field_name="window_end"),
            rounded_duration_seconds=item.rounded_duration_seconds,
            summary=item.summary,
            cadence=item.cadence,
            payload_preview=dict(item.payload_preview),
            payload_sha256=item.payload_sha256,
            entity_shares={} if entity_shares is None else dict(entity_shares),
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize envelope into a JSON-compatible dictionary."""
        return {
            "node_id": self.node_id,
            "outbox_item_id": self.outbox_item_id,
            "idempotency_key": self.idempotency_key,
            "target_ref": self.target_ref,
            "window_start": format_utc_iso(self.window_start),
            "window_end": format_utc_iso(self.window_end),
            "rounded_duration_seconds": self.rounded_duration_seconds,
            "summary": self.summary,
            "cadence": self.cadence,
            "payload_preview": dict(self.payload_preview),
            "payload_sha256": self.payload_sha256,
            "entity_shares": dict(self.entity_shares),
        }


@dataclass(frozen=True, slots=True)
class IngestedHubRollup:
    """Persisted aggregate `Rollup` record stored by `HubRollupIngestStore`."""

    id: str
    node_id: str
    outbox_item_id: str
    idempotency_key: str
    target_ref: str
    window_start: datetime
    window_end: datetime
    rounded_duration_seconds: int
    summary: str
    cadence: str
    payload_sha256: str
    entity_shares: dict[str, float]
    ingested_at: datetime
    deduplicated: bool = False

    def to_dict(self) -> dict[str, object]:
        """Serialize ingested rollup into a JSON-compatible dictionary."""
        data = dataclasses.asdict(self)
        data["window_start"] = format_utc_iso(self.window_start)
        data["window_end"] = format_utc_iso(self.window_end)
        data["ingested_at"] = format_utc_iso(self.ingested_at)
        return data


@dataclass(frozen=True, slots=True)
class CatalogEntityRecord:
    """Shared `Entity` catalog entry published by the central `Hub`."""

    kind: str
    key: str
    name: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    entity_id: str | None = None

    def __post_init__(self) -> None:
        """Validate `kind`, `key`, `name`, and freeze `metadata`."""
        object.__setattr__(self, "kind", validate_lower_slug(self.kind, field_name="Entity kind"))
        object.__setattr__(self, "key", validate_entity_key(self.key))
        object.__setattr__(
            self, "name", validate_non_empty_text(self.name, field_name="Entity name")
        )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        """Serialize catalog entity record to a dictionary."""
        return {
            "kind": self.kind,
            "key": self.key,
            "name": self.name,
            "metadata": dict(self.metadata),
            "entity_id": self.entity_id,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> CatalogEntityRecord:
        """Parse a `CatalogEntityRecord` from a JSON mapping."""
        raw_meta = raw.get("metadata")
        raw_id = raw.get("entity_id", raw.get("id"))
        return cls(
            kind=str(raw.get("kind", "")),
            key=str(raw.get("key", "")),
            name=str(raw.get("name", "")),
            metadata=dict(raw_meta) if isinstance(raw_meta, Mapping) else {},
            entity_id=str(raw_id).strip() if isinstance(raw_id, str) and raw_id.strip() else None,
        )


@dataclass(frozen=True, slots=True)
class CatalogAttributionTemplate:
    """Default `Attribution` edge template published by the central `Hub`."""

    target_kind: str
    target_key: str
    weight: float
    source_kind: str | None = None
    source_key: str | None = None
    source_activity_ref: str | None = None

    def __post_init__(self) -> None:
        """Validate exclusive `Entity` vs `Activity` source and edge weight."""
        clean_t_kind = validate_lower_slug(self.target_kind, field_name="target_kind")
        clean_t_key = validate_entity_key(self.target_key)
        clean_weight = validate_attribution_weight(self.weight)
        has_ent_src = self.source_kind is not None or self.source_key is not None
        if has_ent_src == (self.source_activity_ref is not None):
            raise HubRollupValidationError(
                "Specify either (source_kind, source_key) or source_activity_ref."
            )
        if has_ent_src:
            if self.source_kind is None or self.source_key is None:
                raise HubRollupValidationError("Both source_kind and source_key are required.")
            s_kind = validate_lower_slug(self.source_kind, field_name="source_kind")
            s_key = validate_entity_key(self.source_key)
            if (s_kind, s_key) == (clean_t_kind, clean_t_key):
                raise HubRollupValidationError("Self-referential catalog template is forbidden.")
            object.__setattr__(self, "source_kind", s_kind)
            object.__setattr__(self, "source_key", s_key)
        else:
            assert self.source_activity_ref is not None
            clean_act = validate_non_empty_text(
                self.source_activity_ref, field_name="source_activity_ref"
            )
            object.__setattr__(self, "source_activity_ref", clean_act)
        object.__setattr__(self, "target_kind", clean_t_kind)
        object.__setattr__(self, "target_key", clean_t_key)
        object.__setattr__(self, "weight", clean_weight)

    def to_dict(self) -> dict[str, object]:
        """Serialize attribution template to a JSON-compatible dictionary."""
        return dataclasses.asdict(self)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> CatalogAttributionTemplate:
        """Parse a `CatalogAttributionTemplate` from a JSON mapping."""
        s_kind, s_key, act = (
            raw.get("source_kind"),
            raw.get("source_key"),
            raw.get("source_activity_ref"),
        )
        raw_weight = raw.get("weight", 1.0)
        return cls(
            source_kind=str(s_kind) if isinstance(s_kind, str) and s_kind else None,
            source_key=str(s_key) if isinstance(s_key, str) and s_key else None,
            source_activity_ref=str(act) if isinstance(act, str) and act else None,
            target_kind=str(raw.get("target_kind", "")),
            target_key=str(raw.get("target_key", "")),
            weight=float(raw_weight) if isinstance(raw_weight, (int, float)) else 0.0,
        )


@dataclass(frozen=True, slots=True)
class EntityCatalogSnapshot:
    """Point-in-time snapshot of shared `Entities` and default `Attribution` templates."""

    entities: tuple[CatalogEntityRecord, ...] = ()
    attribution_templates: tuple[CatalogAttributionTemplate, ...] = ()
    catalog_version: int = DEFAULT_CATALOG_VERSION

    def to_dict(self) -> dict[str, object]:
        """Serialize snapshot to a JSON-compatible dictionary."""
        return {
            "catalog_version": self.catalog_version,
            "entities": [ent.to_dict() for ent in self.entities],
            "attribution_templates": [tmpl.to_dict() for tmpl in self.attribution_templates],
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> EntityCatalogSnapshot:
        """Hydrate an `EntityCatalogSnapshot` from a decoded JSON mapping."""
        raw_entities, raw_templates = raw.get("entities", ()), raw.get("attribution_templates", ())
        raw_ver = raw.get("catalog_version", DEFAULT_CATALOG_VERSION)
        return cls(
            entities=tuple(
                CatalogEntityRecord.from_mapping(item)
                for item in (raw_entities if isinstance(raw_entities, list) else ())
                if isinstance(item, Mapping)
            ),
            attribution_templates=tuple(
                CatalogAttributionTemplate.from_mapping(item)
                for item in (raw_templates if isinstance(raw_templates, list) else ())
                if isinstance(item, Mapping)
            ),
            catalog_version=int(raw_ver) if isinstance(raw_ver, int) else DEFAULT_CATALOG_VERSION,
        )


# endregion
