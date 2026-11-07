"""Injectable `WebServiceContainer`, OAuth provider loader, and DRF exception mapper."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from rest_framework import status

if TYPE_CHECKING:
    from rest_framework.response import Response

from worktrack.agenda import AgendaError, AgendaPlanner, AgendaValidationError
from worktrack.config import ConfigurationError, WorktrackSettings
from worktrack.connectors import (
    JiraCloudConfig,
    JiraConfigurationError,
    JiraOAuthClient,
    JiraWorklogSink,
)
from worktrack.core import (
    AttributionCycleError,
    DuplicateKeyError,
    FacetValueValidationError,
    IntervalStateError,
    InvalidAttributionWeightError,
    Ledger,
    LedgerError,
    LedgerValidationError,
    NoActiveIntervalError,
    NotFoundError,
    OverlapConflictError,
)
from worktrack.outbox import (
    EgressBypassBlockedError,
    OutboxDispatcher,
    OutboxError,
    OutboxItemNotFoundError,
    OutboxStateError,
    OutboxStore,
    OutboxValidationError,
    RollupEngine,
    RollupSink,
)
from worktrack.secrets import (
    OAuthBrokerError,
    OAuthInvalidClientError,
    OAuthProviderConfig,
    SecretError,
    SecretResolver,
    StatelessOAuthBroker,
    UserTokenStore,
    scrub_secrets,
)
from worktrack.telemetry import StructuredEventLogger, TelemetryRecorder

# region Constants
ENV_OAUTH_PROVIDERS_JSON: Final = "WORKTRACK_OAUTH_PROVIDERS_JSON"
ENV_OAUTH_PREFIX: Final = "WORKTRACK_OAUTH_"
CLIENT_ID_SUFFIX: Final = "_CLIENT_ID"
NODE_MODE: Final = "node"

_ACTIVE_WEB_SERVICES: ContextVar[WebServiceContainer | None] = ContextVar(
    "worktrack_active_web_services",
    default=None,
)
_DEFAULT_SERVICES_LOCK = threading.Lock()
_DEFAULT_SERVICES_CACHE: list[tuple[tuple[tuple[str, str], ...], WebServiceContainer]] = []
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class WebServiceContainer:
    """Injectable runtime dependency container for `worktrack.web` views and middleware."""

    settings: WorktrackSettings
    telemetry: TelemetryRecorder
    event_logger: StructuredEventLogger
    oauth_broker: StatelessOAuthBroker
    sinks: tuple[RollupSink, ...] = ()
    environ: Mapping[str, str] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: WorktrackSettings,
        *,
        sinks: Iterable[RollupSink] | None = None,
        oauth_broker: StatelessOAuthBroker | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> WebServiceContainer:
        """Build a `WebServiceContainer` wired to `settings`, telemetry, sinks, and OAuth broker."""
        telemetry = TelemetryRecorder.from_settings(settings)
        event_logger = StructuredEventLogger(telemetry_dir=settings.telemetry_dir)
        broker = (
            _build_default_oauth_broker(
                telemetry=telemetry,
                event_logger=event_logger,
                environ=environ,
            )
            if oauth_broker is None
            else oauth_broker
        )
        resolved_sinks = (
            _build_default_sinks(settings=settings, telemetry=telemetry, environ=environ)
            if sinks is None
            else tuple(sinks)
        )
        return cls(
            settings=settings,
            telemetry=telemetry,
            event_logger=event_logger,
            oauth_broker=broker,
            sinks=resolved_sinks,
            environ=environ,
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> WebServiceContainer:
        """Resolve `WorktrackSettings` from `environ` and construct a `WebServiceContainer`."""
        settings = WorktrackSettings.from_env(environ=environ)
        return cls.from_settings(settings, environ=environ)

    def with_oauth_broker(self, broker: StatelessOAuthBroker) -> WebServiceContainer:
        """Return a shallow copy of this container replacing `oauth_broker`."""
        return replace(self, oauth_broker=broker)

    def with_sinks(self, sinks: Iterable[RollupSink]) -> WebServiceContainer:
        """Return a shallow copy of this container replacing registered `sinks`."""
        return replace(self, sinks=tuple(sinks))

    @contextmanager
    def open_ledger(self) -> Iterator[Ledger]:
        """Open a migrated SQLite `Ledger` bound to `self.settings.db_path`."""
        with Ledger.from_settings(
            self.settings,
            telemetry=self.telemetry,
            environ=self.environ,
        ) as ledger:
            yield ledger

    @contextmanager
    def open_planner(self, *, ledger: Ledger | None = None) -> Iterator[AgendaPlanner]:
        """Open a migrated `AgendaPlanner` bound to `self.settings.db_path`."""
        with AgendaPlanner.from_settings(
            self.settings,
            ledger=ledger,
            telemetry=self.telemetry,
            environ=self.environ,
        ) as planner:
            yield planner

    @contextmanager
    def open_outbox(self) -> Iterator[tuple[Ledger, OutboxStore, RollupEngine, OutboxDispatcher]]:
        """Open coordinated `(Ledger, OutboxStore, RollupEngine, OutboxDispatcher)`."""
        with (
            self.open_ledger() as ledger,
            OutboxStore.from_settings(
                self.settings,
                telemetry=self.telemetry,
                environ=self.environ,
            ) as store,
        ):
            engine = RollupEngine(
                ledger=ledger,
                outbox_store=store,
                telemetry=self.telemetry,
            )
            dispatcher = OutboxDispatcher(
                outbox_store=store,
                sinks=self.sinks,
                telemetry=self.telemetry,
                event_logger=self.event_logger,
                environ=self.environ,
            )
            yield ledger, store, engine, dispatcher


# endregion


# region Resolution
@contextmanager
def use_web_services(container: WebServiceContainer) -> Iterator[WebServiceContainer]:
    """Bind `container` as the active `WebServiceContainer` for the enclosed scope."""
    token = _ACTIVE_WEB_SERVICES.set(container)
    try:
        yield container
    finally:
        _ACTIVE_WEB_SERVICES.reset(token)


def reset_default_web_services() -> None:
    """Clear the cached process-level default `WebServiceContainer`."""
    with _DEFAULT_SERVICES_LOCK:
        _DEFAULT_SERVICES_CACHE.clear()


def get_active_web_services() -> WebServiceContainer:
    """Return the active injected `WebServiceContainer` or a cached `os.environ` singleton."""
    injected = _ACTIVE_WEB_SERVICES.get()
    if injected is not None:
        return injected
    snapshot = tuple(sorted(os.environ.items()))
    with _DEFAULT_SERVICES_LOCK:
        if _DEFAULT_SERVICES_CACHE and _DEFAULT_SERVICES_CACHE[0][0] == snapshot:
            return _DEFAULT_SERVICES_CACHE[0][1]
        built = WebServiceContainer.from_env()
        _DEFAULT_SERVICES_CACHE[:] = [(snapshot, built)]
        return built


def load_oauth_provider_configs(
    environ: Mapping[str, str] | None = None,
) -> tuple[OAuthProviderConfig, ...]:
    """Load `OAuthProviderConfig` entries from JSON or `WORKTRACK_OAUTH_<PROVIDER>_*` env vars."""
    env = os.environ if environ is None else environ
    configs: dict[str, OAuthProviderConfig] = {}
    raw_json = env.get(ENV_OAUTH_PROVIDERS_JSON, "").strip()
    if raw_json:
        _parse_json_providers(raw_json, configs)
    _parse_prefixed_env_providers(env, configs)
    return tuple(configs.values())


def _parse_json_providers(
    raw_json: str,
    target: dict[str, OAuthProviderConfig],
) -> None:
    """Parse `WORKTRACK_OAUTH_PROVIDERS_JSON` array into `target`."""
    try:
        decoded = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Invalid JSON in {ENV_OAUTH_PROVIDERS_JSON}.") from exc
    if not isinstance(decoded, list):
        raise ConfigurationError(f"{ENV_OAUTH_PROVIDERS_JSON} must be a JSON array.")
    for entry in decoded:
        if not isinstance(entry, Mapping):
            raise ConfigurationError(f"Each item in {ENV_OAUTH_PROVIDERS_JSON} must be an object.")
        raw_scopes = entry.get("scopes", ())
        scopes = tuple(str(s) for s in raw_scopes) if isinstance(raw_scopes, list) else ()
        cfg = OAuthProviderConfig(
            provider=str(entry.get("provider", "")).strip().lower(),
            client_id=str(entry.get("client_id", "")),
            client_secret_uri=str(entry.get("client_secret_uri", "")),
            token_endpoint=str(entry.get("token_endpoint", "")),
            scopes=scopes,
        )
        target[cfg.provider] = cfg


def _parse_prefixed_env_providers(
    env: Mapping[str, str],
    target: dict[str, OAuthProviderConfig],
) -> None:
    """Discover `WORKTRACK_OAUTH_<PROVIDER>_{CLIENT_ID,CLIENT_SECRET_URI,TOKEN_ENDPOINT}` vars."""
    for key, client_id in env.items():
        slug = _extract_oauth_provider_slug(key)
        if slug is None:
            continue
        built = _build_env_provider_config(env, slug, client_id)
        if built is not None:
            target[slug] = built


def _extract_oauth_provider_slug(env_key: str) -> str | None:
    """Extract lowercase provider slug from `WORKTRACK_OAUTH_<SLUG>_CLIENT_ID`."""
    if not env_key.startswith(ENV_OAUTH_PREFIX) or not env_key.endswith(CLIENT_ID_SUFFIX):
        return None
    slug = env_key.removeprefix(ENV_OAUTH_PREFIX).removesuffix(CLIENT_ID_SUFFIX).lower()
    if not slug or slug == "providers":
        return None
    return slug


def _build_env_provider_config(
    env: Mapping[str, str],
    slug: str,
    client_id: str,
) -> OAuthProviderConfig | None:
    """Build `OAuthProviderConfig` for `slug` when required env vars are non-empty."""
    prefix = f"{ENV_OAUTH_PREFIX}{slug.upper()}"
    clean_id = client_id.strip()
    secret_uri = env.get(f"{prefix}_CLIENT_SECRET_URI", "").strip()
    token_url = env.get(f"{prefix}_TOKEN_ENDPOINT", "").strip()
    if not clean_id or not secret_uri or not token_url:
        return None
    scopes = tuple(env.get(f"{prefix}_SCOPES", "").split())
    return OAuthProviderConfig(
        provider=slug,
        client_id=clean_id,
        client_secret_uri=secret_uri,
        token_endpoint=token_url,
        scopes=scopes,
    )


def _build_default_sinks(
    *,
    settings: WorktrackSettings,
    telemetry: TelemetryRecorder,
    environ: Mapping[str, str] | None,
) -> tuple[RollupSink, ...]:
    """Wire default `JiraWorklogSink` in `node` mode while keeping `hub` mode sink-free."""
    if settings.mode != NODE_MODE:
        return ()
    try:
        client = JiraOAuthClient(
            config=JiraCloudConfig(),
            token_store=UserTokenStore(environ=environ),
            environ=environ,
        )
    except JiraConfigurationError:
        return ()
    return (JiraWorklogSink(client=client, telemetry=telemetry),)


def _build_default_oauth_broker(
    *,
    telemetry: TelemetryRecorder,
    event_logger: StructuredEventLogger,
    environ: Mapping[str, str] | None,
) -> StatelessOAuthBroker:
    """Construct default `StatelessOAuthBroker` from environment secret/provider config."""
    resolver = SecretResolver.from_env(environ=environ, telemetry=telemetry)
    providers = load_oauth_provider_configs(environ=environ)
    return StatelessOAuthBroker(
        secret_resolver=resolver,
        providers=providers,
        telemetry=telemetry,
        event_logger=event_logger,
        environ=environ,
    )


# endregion


# region Validation
def worktrack_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Translate Worktrack domain, Outbox, and OAuth exceptions into scrubbed DRF `Response`s."""
    from rest_framework.response import Response
    from rest_framework.views import exception_handler as drf_default_exception_handler

    drf_resp = drf_default_exception_handler(exc, context)
    if drf_resp is not None:
        return drf_resp
    mapped = _classify_domain_exception(exc)
    if mapped is None:
        return None
    http_status, error_code = mapped
    safe_detail = scrub_secrets(str(exc))
    return Response(
        {"detail": safe_detail, "code": error_code},
        status=http_status,
    )


def _classify_domain_exception(exc: Exception) -> tuple[int, str] | None:
    """Map a Worktrack exception instance to `(http_status_code, error_code)`."""
    if isinstance(exc, (NotFoundError, OutboxItemNotFoundError)):
        return status.HTTP_404_NOT_FOUND, "not_found"
    if isinstance(exc, EgressBypassBlockedError):
        return status.HTTP_403_FORBIDDEN, "egress_bypass_blocked"
    if isinstance(
        exc,
        (
            DuplicateKeyError,
            OverlapConflictError,
            IntervalStateError,
            NoActiveIntervalError,
            AttributionCycleError,
            OutboxStateError,
        ),
    ):
        return status.HTTP_409_CONFLICT, "conflict"
    if isinstance(exc, OAuthInvalidClientError):
        return status.HTTP_502_BAD_GATEWAY, "oauth_invalid_client"
    if isinstance(exc, (OAuthBrokerError, SecretError)):
        return status.HTTP_400_BAD_REQUEST, "oauth_broker_error"
    if isinstance(
        exc,
        (
            LedgerValidationError,
            InvalidAttributionWeightError,
            FacetValueValidationError,
            AgendaValidationError,
            OutboxValidationError,
            ConfigurationError,
            LedgerError,
            AgendaError,
            OutboxError,
        ),
    ):
        return status.HTTP_400_BAD_REQUEST, "validation_error"
    return None


# endregion
