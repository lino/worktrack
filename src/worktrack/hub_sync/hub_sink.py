"""Outbox-gated `GangliaHubSink`, `verify_outbox_grant`, and `HubSyncTransport` adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol

from worktrack.hub_sync.exceptions import (
    HubAuthenticationError,
    HubEgressBypassError,
    HubRollupValidationError,
    HubTransportError,
)
from worktrack.hub_sync.ingest_store import HubRollupIngestStore
from worktrack.hub_sync.models import (
    DEFAULT_ROLLUP_INGEST_PATH,
    HUB_SINK_SLUG,
    IDEMPOTENCY_HEADER_NAME,
    NODE_ID_HEADER_NAME,
    PAYLOAD_SHA256_HEADER_NAME,
    TRACE_ID_HEADER_NAME,
    GangliaRollupEnvelope,
    HubEndpointConfig,
    HubHttpResponse,
    validate_ganglia_rollup_payload,
    validate_hub_url,
)
from worktrack.outbox.exceptions import EgressBypassBlockedError
from worktrack.outbox.grants import (
    ApprovedOutboxGrant,
    enforce_outbox_grant,
    sanitize_bypass_label,
)
from worktrack.outbox.models import OutboxItem, SinkWriteReceipt
from worktrack.secrets import OAuthTokenBundle, SecretError, UserTokenStore
from worktrack.telemetry.logging import resolve_trace_id
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
HTTP_STATUS_OK: Final = 200
HTTP_STATUS_CREATED: Final = 201
HTTP_STATUS_OK_MIN: Final = 200
HTTP_STATUS_OK_MAX: Final = 299
HTTP_STATUS_UNAUTHORIZED: Final = 401
JSON_CONTENT_TYPE: Final = "application/json"
RECEIPT_ID_PREFIX_LEN: Final = 16
# endregion


# region Protocols
class HubSyncTransport(Protocol):
    """Injectable transport protocol for `Node` -> `Hub` rollup push and catalog pull."""

    def push_rollup(
        self, payload: Mapping[str, object], *, headers: Mapping[str, str] | None = None
    ) -> HubHttpResponse:
        """Push an approved Ganglia aggregate `Rollup` envelope to the `Hub`."""
        ...

    def pull_entity_catalog(self, *, headers: Mapping[str, str] | None = None) -> HubHttpResponse:
        """Pull shared `Entity` catalog and default `Attribution` templates from the `Hub`."""
        ...


# endregion


# region Grants
def verify_outbox_grant(
    *,
    item: object,
    grant: ApprovedOutboxGrant | None,
    sink_name: str = HUB_SINK_SLUG,
    telemetry: TelemetryRecorder | None = None,
    source: str = "direct_call",
) -> OutboxItem:
    """Verify `ApprovedOutboxGrant` and strict Ganglia aggregate `Rollup` payload hygiene."""
    if isinstance(item, OutboxItem):
        try:
            validate_ganglia_rollup_payload(item.payload_preview)
        except HubRollupValidationError as exc:
            _record_bypass_and_raise(sink_name, source, telemetry, str(exc))
    try:
        enforce_outbox_grant(
            sink_name=sink_name, item=item, grant=grant, telemetry=telemetry, source=source
        )
    except EgressBypassBlockedError as exc:
        raise HubEgressBypassError(str(exc)) from exc

    assert isinstance(item, OutboxItem)
    if item.sink != sink_name:
        _record_bypass_and_raise(
            sink_name, source, telemetry, f"OutboxItem sink '{item.sink}' != '{sink_name}'."
        )
    return item


def _record_bypass_and_raise(
    sink_name: str, source: str, telemetry: TelemetryRecorder | None, reason: str
) -> None:
    """Increment `worktrack_egress_bypass_blocked_total` and raise `HubEgressBypassError`."""
    if telemetry is not None:
        telemetry.record_egress_bypass_blocked(
            sink=sanitize_bypass_label(sink_name),
            source=sanitize_bypass_label(source),
        )
    raise HubEgressBypassError(reason)


# endregion


# region HTTP Transport
class UrllibHubSyncTransport:
    """Stdlib `urllib.request` `HubSyncTransport` propagating `X-Fritz-Trace-Id` and bearer auth."""

    def __init__(
        self,
        *,
        config: HubEndpointConfig | None = None,
        token_store: UserTokenStore | None = None,
        require_auth: bool = False,
        environ: Mapping[str, str] | None = None,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        """Initialize HTTP transport with `HubEndpointConfig` and optional `UserTokenStore`."""
        self._config = HubEndpointConfig() if config is None else config
        self._token_store = token_store
        self._require_auth = require_auth
        self._environ = environ
        self._urlopen = urlopen

    def push_rollup(
        self, payload: Mapping[str, object], *, headers: Mapping[str, str] | None = None
    ) -> HubHttpResponse:
        """POST an aggregate `Rollup` envelope to the Hub ingest endpoint."""
        validate_ganglia_rollup_payload(payload)
        url = self._config.build_url(self._config.rollup_ingest_path, environ=self._environ)
        return self.request("POST", url, headers=headers, json_body=payload)

    def pull_entity_catalog(self, *, headers: Mapping[str, str] | None = None) -> HubHttpResponse:
        """GET shared `Entity` catalog and default `Attribution` templates from the Hub."""
        url = self._config.build_url(self._config.catalog_pull_path, environ=self._environ)
        return self.request("GET", url, headers=headers, json_body=None)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> HubHttpResponse:
        """Execute HTTP request with `X-Fritz-Trace-Id` and bearer auth from `UserTokenStore`."""
        validate_hub_url(url)
        timeout = (
            self._config.resolve_timeout(environ=self._environ)
            if timeout_seconds is None
            else timeout_seconds
        )
        req_headers, redact_vals = self._prepare_headers(headers)
        body_bytes = None if json_body is None else json.dumps(json_body).encode("utf-8")
        if body_bytes is not None:
            req_headers.setdefault("Content-Type", JSON_CONTENT_TYPE)
        req = urllib.request.Request(
            url, data=body_bytes, headers=req_headers, method=method.upper()
        )
        return self._dispatch_urllib_request(req, url, timeout, redact_vals)

    def _prepare_headers(
        self, extra_headers: Mapping[str, str] | None
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """Assemble request headers with `X-Fritz-Trace-Id` and `UserTokenStore` bearer token."""
        merged: dict[str, str] = {"Accept": JSON_CONTENT_TYPE}
        if extra_headers is not None:
            merged.update(extra_headers)
        lower_keys = {k.lower() for k in merged}
        if TRACE_ID_HEADER_NAME.lower() not in lower_keys:
            merged[TRACE_ID_HEADER_NAME] = resolve_trace_id(environ=self._environ)
        redact: list[str] = []
        if "authorization" not in lower_keys:
            bundle = self._load_token_bundle()
            if bundle is not None:
                token_str = bundle.access_token.expose()
                merged["Authorization"] = f"{bundle.token_type} {token_str}"
                redact.append(token_str)
        for key, val in merged.items():
            if key.lower() == "authorization":
                redact.append(val)
        return merged, tuple(redact)

    def _load_token_bundle(self) -> OAuthTokenBundle | None:
        """Load `OAuthTokenBundle` from `UserTokenStore` if configured."""
        if self._token_store is None:
            if self._require_auth:
                raise HubAuthenticationError(
                    "Hub authentication required, but token_store is None."
                )
            return None
        try:
            bundle = self._token_store.load(self._config.provider_name, self._config.account)
        except SecretError as exc:
            raise HubAuthenticationError(f"Failed to read Hub token: {exc}") from exc
        if bundle is None and self._require_auth:
            raise HubAuthenticationError(
                f"No OAuth token found in UserTokenStore for '{self._config.provider_name}'."
            )
        return bundle

    def _dispatch_urllib_request(
        self,
        req: urllib.request.Request,
        url: str,
        timeout: float,
        redact_vals: tuple[str, ...],
    ) -> HubHttpResponse:
        """Invoke `self._urlopen` and decode JSON response or raise `HubTransportError`."""
        try:
            with self._urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                resp_headers = dict(resp.headers.items()) if hasattr(resp, "headers") else {}
                status_code = int(getattr(resp, "status", HTTP_STATUS_OK))
        except urllib.error.HTTPError as exc:
            err_payload = _decode_hub_json(exc.read(), redact_vals, url=url, allow_non_json=True)
            if int(exc.code) == HTTP_STATUS_UNAUTHORIZED:
                raise HubAuthenticationError(
                    f"Hub rejected bearer token with HTTP 401 at '{url}'.",
                    http_status=HTTP_STATUS_UNAUTHORIZED,
                    destination_uri=url,
                    redact=redact_vals,
                ) from exc
            raise HubTransportError(
                f"Hub HTTP {exc.code} error calling '{url}': {err_payload}",
                http_status=int(exc.code),
                destination_uri=url,
                redact=redact_vals,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HubTransportError(
                f"Hub network transport error calling '{url}'.",
                destination_uri=url,
                redact=redact_vals,
            ) from exc

        payload = _decode_hub_json(raw, redact_vals, url=url, allow_non_json=False)
        if status_code == HTTP_STATUS_UNAUTHORIZED:
            raise HubAuthenticationError(
                f"Hub rejected bearer token with HTTP 401 at '{url}'.",
                http_status=HTTP_STATUS_UNAUTHORIZED,
                destination_uri=url,
                redact=redact_vals,
            )
        if not (HTTP_STATUS_OK_MIN <= status_code <= HTTP_STATUS_OK_MAX):
            raise HubTransportError(
                f"Hub returned HTTP {status_code} for '{url}'.",
                http_status=status_code,
                destination_uri=url,
                redact=redact_vals,
            )
        return HubHttpResponse(status_code=status_code, payload=payload, headers=resp_headers)


def _decode_hub_json(
    raw_bytes: bytes,
    redact_vals: tuple[str, ...],
    *,
    url: str,
    allow_non_json: bool,
) -> Mapping[str, object]:
    """Decode UTF-8 JSON mapping from Hub HTTP response bytes."""
    if not raw_bytes.strip():
        return {}
    text = raw_bytes.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        if allow_non_json:
            return {"detail": text}
        raise HubTransportError(
            "Hub endpoint returned invalid JSON.",
            destination_uri=url,
            redact=(*redact_vals, text),
        ) from exc
    return parsed if isinstance(parsed, Mapping) else {"items": parsed}


class LoopbackHubSyncTransport:
    """In-process `HubSyncTransport` backed directly by a `HubRollupIngestStore`."""

    def __init__(self, *, ingest_store: HubRollupIngestStore) -> None:
        """Initialize loopback transport bound to `ingest_store`."""
        self._ingest_store = ingest_store
        self.pushed_rollups: list[tuple[dict[str, object], dict[str, str]]] = []
        self.catalog_pulls: list[dict[str, str]] = []

    def push_rollup(
        self, payload: Mapping[str, object], *, headers: Mapping[str, str] | None = None
    ) -> HubHttpResponse:
        """Validate and ingest `payload` into `HubRollupIngestStore`."""
        hdr_dict = dict(headers) if headers is not None else {}
        ingested = self._ingest_store.ingest_rollup(
            payload, expected_sha256=hdr_dict.get(PAYLOAD_SHA256_HEADER_NAME)
        )
        self.pushed_rollups.append((dict(payload), hdr_dict))
        return HubHttpResponse(
            status_code=HTTP_STATUS_OK if ingested.deduplicated else HTTP_STATUS_CREATED,
            payload=ingested.to_dict(),
            headers={TRACE_ID_HEADER_NAME: hdr_dict.get(TRACE_ID_HEADER_NAME, "")},
        )

    def pull_entity_catalog(self, *, headers: Mapping[str, str] | None = None) -> HubHttpResponse:
        """Serve `EntityCatalogSnapshot` from `HubRollupIngestStore`."""
        hdr_dict = dict(headers) if headers is not None else {}
        self.catalog_pulls.append(hdr_dict)
        snapshot = self._ingest_store.get_catalog_snapshot()
        return HubHttpResponse(
            status_code=HTTP_STATUS_OK,
            payload=snapshot.to_dict(),
            headers={TRACE_ID_HEADER_NAME: hdr_dict.get(TRACE_ID_HEADER_NAME, "")},
        )


# endregion


# region Hub Sink
class GangliaHubSink:
    """Outbox-gated `RollupSink` (`sink_name = 'hub'`) pushing aggregate rollups to a `Hub`."""

    def __init__(
        self,
        *,
        transport: HubSyncTransport | None = None,
        config: HubEndpointConfig | None = None,
        token_store: UserTokenStore | None = None,
        telemetry: TelemetryRecorder | None = None,
        environ: Mapping[str, str] | None = None,
        entity_shares_provider: Callable[[OutboxItem], Mapping[str, float]] | None = None,
    ) -> None:
        """Initialize `GangliaHubSink` with injectable `HubSyncTransport` and telemetry."""
        self._config = HubEndpointConfig(base_url="https://127.0.0.1") if config is None else config
        self._token_store = token_store
        self._telemetry = telemetry
        self._environ = environ
        self._entity_shares_provider = entity_shares_provider
        self._transport: HubSyncTransport = (
            UrllibHubSyncTransport(config=self._config, token_store=token_store, environ=environ)
            if transport is None
            else transport
        )

    @property
    def sink_name(self) -> str:
        """Return canonical Hub sink slug (`'hub'`)."""
        return HUB_SINK_SLUG

    def write_rollup(
        self,
        item: OutboxItem,
        grant: ApprovedOutboxGrant | None = None,
        *,
        source: str = "direct_call",
    ) -> SinkWriteReceipt:
        """Verify `ApprovedOutboxGrant`, enforce aggregate rollup hygiene, and push to `Hub`."""
        verified_item = verify_outbox_grant(
            item=item,
            grant=grant,
            sink_name=self.sink_name,
            telemetry=self._telemetry,
            source=source,
        )
        node_id = self._config.resolve_node_id(environ=self._environ)
        shares = (
            {}
            if self._entity_shares_provider is None
            else dict(self._entity_shares_provider(verified_item))
        )
        envelope = GangliaRollupEnvelope.from_outbox_item(
            verified_item, node_id=node_id, entity_shares=shares
        )
        envelope_dict = envelope.to_dict()
        validate_ganglia_rollup_payload(envelope_dict)

        headers = self._build_outbound_headers(verified_item, node_id)
        response = self._transport.push_rollup(envelope_dict, headers=headers)
        destination_uri = self._resolve_destination_uri()
        if not (HTTP_STATUS_OK_MIN <= response.status_code <= HTTP_STATUS_OK_MAX):
            raise HubTransportError(
                f"Hub rollup push failed with HTTP {response.status_code}.",
                http_status=response.status_code,
                destination_uri=destination_uri,
            )
        return SinkWriteReceipt(
            destination_uri=destination_uri,
            http_status=response.status_code,
            external_receipt_id=_extract_receipt_id(
                response.payload, node_id, verified_item.idempotency_key
            ),
        )

    def _build_outbound_headers(self, item: OutboxItem, node_id: str) -> dict[str, str]:
        """Build trace, idempotency, node ID, SHA-256, and optional bearer headers."""
        env = (
            self._environ
            if self._environ is not None
            else getattr(self._transport, "_environ", None)
        )
        headers: dict[str, str] = {
            TRACE_ID_HEADER_NAME: resolve_trace_id(environ=env),
            IDEMPOTENCY_HEADER_NAME: item.idempotency_key,
            NODE_ID_HEADER_NAME: node_id,
            PAYLOAD_SHA256_HEADER_NAME: item.payload_sha256,
        }
        if self._token_store is not None:
            bundle = self._token_store.load(self._config.provider_name, self._config.account)
            if bundle is not None:
                headers["Authorization"] = f"{bundle.token_type} {bundle.access_token.expose()}"
        return headers

    def _resolve_destination_uri(self) -> str:
        """Resolve destination URI for `EgressLedger` receipt recording."""
        if self._config.base_url:
            return self._config.build_url(self._config.rollup_ingest_path, environ=self._environ)
        return f"hub://{self._config.resolve_node_id(environ=self._environ)}{DEFAULT_ROLLUP_INGEST_PATH}"


def _extract_receipt_id(payload: Mapping[str, object], node_id: str, idempotency_key: str) -> str:
    """Extract remote receipt ID from Hub response or synthesize deterministic ID."""
    for field_name in ("id", "receipt_id", "external_receipt_id"):
        raw_val = payload.get(field_name)
        if raw_val is not None and str(raw_val).strip():
            return str(raw_val).strip()
    return f"hub:{node_id}:{idempotency_key[:RECEIPT_ID_PREFIX_LEN]}"


# endregion
