"""Pluggable `SecretProvider` implementations and TTL-caching `SecretResolver`."""

from __future__ import annotations

import contextlib
import importlib
import math
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from worktrack.secrets.aws_sm import AwsSecretsManagerProvider
from worktrack.secrets.exceptions import (
    SecretError,
    SecretNotFoundError,
    SecretPermissionError,
    SecretProviderUnavailableError,
    SecretResolutionError,
    SecretUriError,
    SecretValue,
)
from worktrack.secrets.uri import (
    SCHEME_ENV,
    SCHEME_FILE,
    SCHEME_KEYRING,
    SecretUri,
    extract_safe_metric_scheme,
)
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
DEFAULT_SECRET_CACHE_TTL_SECONDS: float = 300.0
MIN_CACHE_TTL_SECONDS: float = 0.0
ENV_SECRET_CACHE_TTL_SECONDS = "WORKTRACK_SECRET_CACHE_TTL_SECONDS"
ENV_STRICT_SECRET_FILE_PERMISSIONS = "WORKTRACK_STRICT_SECRET_FILE_PERMISSIONS"
DEFAULT_ENFORCE_FILE_PERMISSIONS = False
GROUP_WORLD_PERMISSION_MASK = 0o077
KEYRING_MODULE_NAME = "keyring"

OUTCOME_OK = "ok"
OUTCOME_CACHE_HIT = "cache_hit"
OUTCOME_ERROR = "error"
TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})
# endregion


# region Protocols
class KeyringLookupBackend(Protocol):
    """Read-only structural protocol for OS keyring password lookups."""

    def get_password(self, service: str, account: str, /) -> str | None:
        """Return stored secret for `(service, account)` or `None` if absent."""
        ...


class KeyringBackend(KeyringLookupBackend, Protocol):
    """Full structural protocol for workstation OS keyring read/write/delete."""

    def set_password(self, service: str, account: str, password: str, /) -> None:
        """Store `password` under `(service, account)` in OS keyring."""
        ...

    def delete_password(self, service: str, account: str, /) -> None:
        """Delete stored password under `(service, account)` from OS keyring."""
        ...


class SecretProvider(Protocol):
    """Pluggable Service Provider Interface for scheme-specific secret resolution."""

    @property
    def scheme(self) -> str:
        """Return the canonical URI scheme handled by this provider."""
        ...

    def resolve(self, uri: SecretUri) -> SecretValue:
        """Resolve a validated `SecretUri` into an immutable `SecretValue`."""
        ...


# endregion


# region Providers
class EnvSecretProvider:
    """Resolve `env://VAR_NAME` secrets from injectable mapping or `os.environ`."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        """Initialize provider with optional environment dictionary."""
        self._environ = environ

    @property
    def scheme(self) -> str:
        """Return canonical URI scheme (`env`)."""
        return SCHEME_ENV

    def resolve(self, uri: SecretUri) -> SecretValue:
        """Read non-empty secret value from environment variable."""
        if uri.scheme != SCHEME_ENV or not uri.env_var:
            raise SecretUriError("EnvSecretProvider requires an 'env://' URI.")
        env = os.environ if self._environ is None else self._environ
        raw_value = env.get(uri.env_var)
        if raw_value is None or not raw_value.strip():
            raise SecretNotFoundError(f"Environment secret '{uri.env_var}' is unset or empty.")
        return SecretValue(raw_value)


class FileSecretProvider:
    """Resolve `file:///abs/path` secrets with optional POSIX mode checks."""

    def __init__(
        self,
        *,
        enforce_permissions: bool | None = None,
        max_mode_mask: int = GROUP_WORLD_PERMISSION_MASK,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize file secret provider.

        Args:
            enforce_permissions: Reject group/world-accessible secret files when True.
                Defaults to `WORKTRACK_STRICT_SECRET_FILE_PERMISSIONS` or `False`.
            max_mode_mask: Forbidden permission bitmask (defaults to `0o077`).
            environ: Optional environment mapping consulted when `enforce_permissions` is None.
        """
        if enforce_permissions is None:
            env = os.environ if environ is None else environ
            raw_flag = env.get(ENV_STRICT_SECRET_FILE_PERMISSIONS, "").strip().lower()
            enforce_permissions = (
                raw_flag in TRUTHY_ENV_VALUES if raw_flag else DEFAULT_ENFORCE_FILE_PERMISSIONS
            )
        self._enforce_permissions = enforce_permissions
        self._max_mode_mask = max_mode_mask

    @property
    def scheme(self) -> str:
        """Return canonical URI scheme (`file`)."""
        return SCHEME_FILE

    def resolve(self, uri: SecretUri) -> SecretValue:
        """Read UTF-8 trimmed secret content from an absolute local file."""
        if uri.scheme != SCHEME_FILE or uri.file_path is None:
            raise SecretUriError("FileSecretProvider requires a 'file://' URI.")
        path = uri.file_path
        _verify_secret_file_stat(path, self._enforce_permissions, self._max_mode_mask)
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise SecretResolutionError(f"Failed to read secret file '{path.name}'.") from exc
        if not content:
            raise SecretNotFoundError(f"Secret file '{path.name}' is empty.")
        return SecretValue(content)


class KeyringSecretProvider:
    """Resolve `keyring://service/account` secrets via OS keyring or injected backend."""

    def __init__(
        self,
        *,
        keyring_backend: KeyringLookupBackend | None = None,
        module_loader: Callable[[str], Any] = importlib.import_module,
    ) -> None:
        """Initialize keyring secret provider."""
        self._keyring_backend = keyring_backend
        self._module_loader = module_loader

    @property
    def scheme(self) -> str:
        """Return canonical URI scheme (`keyring`)."""
        return SCHEME_KEYRING

    def resolve(self, uri: SecretUri) -> SecretValue:
        """Look up `(service, account)` password in `KeyringLookupBackend`."""
        if uri.scheme != SCHEME_KEYRING or not uri.keyring_service or not uri.keyring_account:
            raise SecretUriError("KeyringSecretProvider requires a 'keyring://' URI.")
        backend = self._resolve_backend()
        try:
            secret = backend.get_password(uri.keyring_service, uri.keyring_account)
        except Exception as exc:
            raise SecretResolutionError(
                f"Keyring lookup failed for service '{uri.keyring_service}'."
            ) from exc
        if secret is None or not secret.strip():
            raise SecretNotFoundError(
                f"No keyring secret stored under '{uri.keyring_service}/{uri.keyring_account}'."
            )
        return SecretValue(secret)

    def _resolve_backend(self) -> KeyringLookupBackend:
        """Return injected keyring backend or dynamically import `keyring`."""
        if self._keyring_backend is not None:
            return self._keyring_backend
        try:
            loaded: KeyringLookupBackend = self._module_loader(KEYRING_MODULE_NAME)
        except ImportError as exc:
            raise SecretProviderUnavailableError(
                "OS keyring backend is unavailable; install 'keyring' or inject KeyringBackend."
            ) from exc
        self._keyring_backend = loaded
        return loaded


# endregion


# region Resolution
@dataclass(frozen=True, slots=True)
class _CachedSecretEntry:
    """Internal TTL cache record pairing a `SecretValue` with monotonic expiry."""

    value: SecretValue
    expires_at: float
    scheme: str


class SecretResolver:
    """Pluggable `SecretProvider` registry with in-memory TTL cache and eviction."""

    def __init__(
        self,
        *,
        providers: Iterable[SecretProvider] | None = None,
        ttl_seconds: float = DEFAULT_SECRET_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        telemetry: TelemetryRecorder | None = None,
    ) -> None:
        """Initialize `SecretResolver` with scheme providers and TTL cache.

        Args:
            providers: Optional iterable of `SecretProvider` instances. Defaults to
                `env`, `file`, `keyring`, and `aws-sm` providers.
            ttl_seconds: In-memory cache TTL in seconds (`0.0` disables caching).
            clock: Monotonic clock callable for deterministic TTL testing.
            telemetry: Optional `TelemetryRecorder` recording resolve outcomes.
        """
        if not math.isfinite(ttl_seconds) or ttl_seconds < MIN_CACHE_TTL_SECONDS:
            raise ValueError("ttl_seconds must be finite and >= 0.")
        default_providers: tuple[SecretProvider, ...] = (
            EnvSecretProvider(),
            FileSecretProvider(),
            KeyringSecretProvider(),
            AwsSecretsManagerProvider(),
        )
        selected = default_providers if providers is None else tuple(providers)
        self._providers: dict[str, SecretProvider] = {prov.scheme: prov for prov in selected}
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._telemetry = telemetry
        self._cache: dict[str, _CachedSecretEntry] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_env(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        telemetry: TelemetryRecorder | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> SecretResolver:
        """Construct `SecretResolver` honoring environment overrides."""
        env = os.environ if environ is None else environ
        raw_ttl = env.get(ENV_SECRET_CACHE_TTL_SECONDS, "").strip()
        ttl = float(raw_ttl) if raw_ttl else DEFAULT_SECRET_CACHE_TTL_SECONDS
        return cls(
            providers=(
                EnvSecretProvider(environ=environ),
                FileSecretProvider(environ=environ),
                KeyringSecretProvider(),
                AwsSecretsManagerProvider(environ=environ),
            ),
            ttl_seconds=ttl,
            clock=clock,
            telemetry=telemetry,
        )

    def register(self, provider: SecretProvider) -> None:
        """Register or replace a `SecretProvider` for `provider.scheme`."""
        with self._lock:
            self._providers[provider.scheme] = provider

    def resolve(self, uri: str | SecretUri) -> SecretValue:
        """Resolve `uri` via TTL cache or registered `SecretProvider`."""
        raw_key = uri.strip() if isinstance(uri, str) else uri.raw
        cached = self._lookup_cached(raw_key)
        if cached is not None:
            self._record_metric(cached.scheme, OUTCOME_CACHE_HIT)
            return cached.value

        parsed = self._parse_or_record_error(uri, raw_key)
        cached_normalized = self._lookup_cached(parsed.raw) if parsed.raw != raw_key else None
        if cached_normalized is not None:
            self._record_metric(cached_normalized.scheme, OUTCOME_CACHE_HIT)
            return cached_normalized.value

        return self._resolve_uncached(parsed)

    def invalidate(self, uri: str | SecretUri) -> bool:
        """Evict cached entry for `uri` following secret rotation."""
        candidates = {uri.strip()} if isinstance(uri, str) else {uri.raw}
        if isinstance(uri, str):
            with contextlib.suppress(SecretUriError):
                candidates.add(SecretUri.parse(uri).raw)
        evicted = False
        with self._lock:
            for key in candidates:
                if self._cache.pop(key, None) is not None:
                    evicted = True
        return evicted

    def clear_cache(self) -> None:
        """Evict all cached `SecretValue` entries from memory."""
        with self._lock:
            self._cache.clear()

    def _lookup_cached(self, key: str) -> _CachedSecretEntry | None:
        """Return unexpired `_CachedSecretEntry` for `key` or `None`."""
        if self._ttl_seconds <= MIN_CACHE_TTL_SECONDS:
            return None
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if self._clock() < entry.expires_at:
                return entry
            self._cache.pop(key, None)
            return None

    def _parse_or_record_error(self, uri: str | SecretUri, raw_key: str) -> SecretUri:
        """Parse string URI or record bounded error telemetry on malformed input."""
        if isinstance(uri, SecretUri):
            return uri
        try:
            return SecretUri.parse(raw_key)
        except SecretUriError:
            self._record_metric(extract_safe_metric_scheme(raw_key), OUTCOME_ERROR)
            raise

    def _resolve_uncached(self, parsed: SecretUri) -> SecretValue:
        """Invoke backing `SecretProvider`, populate TTL cache, and emit metric."""
        provider = self._providers.get(parsed.scheme)
        if provider is None:
            self._record_metric(parsed.scheme, OUTCOME_ERROR)
            raise SecretUriError(f"No SecretProvider registered for scheme '{parsed.scheme}'.")
        try:
            secret = provider.resolve(parsed)
        except SecretError:
            self._record_metric(parsed.scheme, OUTCOME_ERROR)
            raise
        except Exception as exc:
            self._record_metric(parsed.scheme, OUTCOME_ERROR)
            raise SecretResolutionError(
                f"Unexpected failure resolving '{parsed.scheme}://' secret."
            ) from exc

        if self._ttl_seconds > MIN_CACHE_TTL_SECONDS:
            with self._lock:
                self._cache[parsed.raw] = _CachedSecretEntry(
                    value=secret,
                    expires_at=self._clock() + self._ttl_seconds,
                    scheme=parsed.scheme,
                )
        self._record_metric(parsed.scheme, OUTCOME_OK)
        return secret

    def _record_metric(self, scheme: str, outcome: str) -> None:
        """Record bounded `worktrack_secret_provider_resolves_total` sample if wired."""
        if self._telemetry is not None:
            self._telemetry.record_secret_resolve(scheme=scheme, outcome=outcome)


class CachingSecretProvider(SecretResolver):
    """Alias subclass of `SecretResolver` providing TTL caching and eviction."""


def _verify_secret_file_stat(path: Path, enforce_permissions: bool, max_mode_mask: int) -> None:
    """Validate secret file existence, regular-file type, and optional POSIX mode."""
    try:
        file_stat = path.stat()
    except FileNotFoundError as exc:
        raise SecretNotFoundError(f"Secret file '{path.name}' does not exist.") from exc
    except OSError as exc:
        raise SecretResolutionError(f"Cannot stat secret file '{path.name}'.") from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise SecretResolutionError(f"Secret path '{path.name}' is not a regular file.")
    if enforce_permissions and (stat.S_IMODE(file_stat.st_mode) & max_mode_mask) != 0:
        mode_octal = oct(stat.S_IMODE(file_stat.st_mode))
        raise SecretPermissionError(
            f"Secret file '{path.name}' has insecure permissions ({mode_octal}); expected 0o600."
        )


# endregion
