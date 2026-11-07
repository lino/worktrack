"""Workstation-local `UserTokenStore` backed by OS Keyring with `0o600` file fallback."""

from __future__ import annotations

import importlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from worktrack.config import APP_SLUG, WorktrackSettings
from worktrack.secrets.broker import DEFAULT_TOKEN_TYPE, OAuthTokenBundle
from worktrack.secrets.exceptions import (
    SecretPermissionError,
    SecretValue,
    UserTokenStoreError,
)
from worktrack.secrets.providers import (
    GROUP_WORLD_PERMISSION_MASK,
    KEYRING_MODULE_NAME,
    KeyringBackend,
)

# region Constants
DEFAULT_KEYRING_SERVICE = APP_SLUG
DEFAULT_TOKEN_ACCOUNT = "default"
DEFAULT_TOKEN_STORE_FILENAME = "oauth_tokens.json"
ENV_TOKEN_STORE_PATH = "WORKTRACK_TOKEN_STORE_PATH"

TOKEN_FILE_MODE = 0o600
TOKEN_DIR_MODE = 0o700
JSON_INDENT_SPACES = 2
# endregion


# region Token Store
class UserTokenStore:
    """Store, load, and delete per-user `OAuthTokenBundle` credentials locally.

    Credentials reside solely on the user's workstation in the OS Keyring
    (`KeyringBackend`), falling back to an atomic POSIX `0o600` JSON file for
    headless shell environments when `allow_file_fallback=True`.
    """

    def __init__(
        self,
        *,
        keyring_backend: KeyringBackend | None = None,
        fallback_path: Path | None = None,
        service_namespace: str = DEFAULT_KEYRING_SERVICE,
        allow_file_fallback: bool = True,
        remediate_permissions: bool = False,
        environ: Mapping[str, str] | None = None,
        module_loader: Callable[[str], Any] = importlib.import_module,
    ) -> None:
        """Initialize workstation-local `UserTokenStore`.

        Args:
            keyring_backend: Optional injected `KeyringBackend` implementation.
            fallback_path: Path to POSIX `0o600` fallback file for headless shells.
            service_namespace: Base OS keyring service prefix (`worktrack`).
            allow_file_fallback: Permit `0o600` local file storage when keyring fails.
            remediate_permissions: Tighten group/world-readable fallback files to
                `0o600` on load when True; reject with `SecretPermissionError` when False.
            environ: Optional environment mapping consulted for default path resolution.
            module_loader: Dynamic module loader defaulting to `importlib.import_module`.
        """
        self._keyring_backend = keyring_backend
        self._fallback_path = _resolve_default_fallback_path(fallback_path, environ)
        self._service_namespace = service_namespace.strip() or DEFAULT_KEYRING_SERVICE
        self._allow_file_fallback = allow_file_fallback
        self._remediate_permissions = remediate_permissions
        self._module_loader = module_loader

    @classmethod
    def from_settings(
        cls,
        settings: WorktrackSettings,
        *,
        keyring_backend: KeyringBackend | None = None,
        allow_file_fallback: bool = True,
        remediate_permissions: bool = False,
        module_loader: Callable[[str], Any] = importlib.import_module,
    ) -> UserTokenStore:
        """Construct `UserTokenStore` anchored under `settings.config_file.parent`."""
        return cls(
            keyring_backend=keyring_backend,
            fallback_path=settings.config_file.parent / DEFAULT_TOKEN_STORE_FILENAME,
            allow_file_fallback=allow_file_fallback,
            remediate_permissions=remediate_permissions,
            module_loader=module_loader,
        )

    @property
    def fallback_path(self) -> Path:
        """Return configured POSIX `0o600` fallback file path."""
        return self._fallback_path

    def store(
        self,
        provider: str,
        bundle: OAuthTokenBundle,
        account: str = DEFAULT_TOKEN_ACCOUNT,
    ) -> None:
        """Persist `bundle` for `(provider, account)` in OS keyring or `0o600` fallback."""
        provider_key, account_key = _validate_store_keys(provider, account)
        serialized = _serialize_bundle(bundle)
        backend = self._try_get_keyring_backend()
        if backend is not None:
            try:
                backend.set_password(self._keyring_service(provider_key), account_key, serialized)
                return
            except Exception as exc:
                if not self._allow_file_fallback:
                    raise UserTokenStoreError(
                        f"Failed to store OAuth token in OS keyring for '{provider_key}'.",
                        redact=(serialized,),
                    ) from exc

        self._require_fallback_allowed(provider_key)
        data = _read_fallback_map(self._fallback_path, self._remediate_permissions)
        data.setdefault(provider_key, {})[account_key] = json.loads(serialized)
        _write_fallback_map_atomic(self._fallback_path, data)

    def load(
        self,
        provider: str,
        account: str = DEFAULT_TOKEN_ACCOUNT,
    ) -> OAuthTokenBundle | None:
        """Load `OAuthTokenBundle` for `(provider, account)`, or `None` if absent."""
        provider_key, account_key = _validate_store_keys(provider, account)
        backend = self._try_get_keyring_backend()
        if backend is not None:
            try:
                raw_json = backend.get_password(self._keyring_service(provider_key), account_key)
                return None if raw_json is None else _deserialize_bundle_json(raw_json)
            except UserTokenStoreError:
                raise
            except Exception as exc:
                if not self._allow_file_fallback:
                    raise UserTokenStoreError(
                        f"Failed to load OAuth token from OS keyring for '{provider_key}'."
                    ) from exc

        self._require_fallback_allowed(provider_key)
        data = _read_fallback_map(self._fallback_path, self._remediate_permissions)
        entry = data.get(provider_key, {}).get(account_key)
        if entry is None:
            return None
        return _deserialize_bundle_mapping(entry)

    def delete(
        self,
        provider: str,
        account: str = DEFAULT_TOKEN_ACCOUNT,
    ) -> bool:
        """Delete stored credentials for `(provider, account)`, returning True if removed."""
        provider_key, account_key = _validate_store_keys(provider, account)
        backend = self._try_get_keyring_backend()
        if backend is not None:
            service = self._keyring_service(provider_key)
            try:
                existing = backend.get_password(service, account_key)
                if existing is None:
                    return False
                backend.delete_password(service, account_key)
                return True
            except Exception as exc:
                if not self._allow_file_fallback:
                    raise UserTokenStoreError(
                        f"Failed to delete OAuth token from OS keyring for '{provider_key}'."
                    ) from exc

        self._require_fallback_allowed(provider_key)
        data = _read_fallback_map(self._fallback_path, self._remediate_permissions)
        provider_bucket = data.get(provider_key)
        if not provider_bucket or account_key not in provider_bucket:
            return False
        provider_bucket.pop(account_key, None)
        if not provider_bucket:
            data.pop(provider_key, None)
        _write_fallback_map_atomic(self._fallback_path, data)
        return True

    def _keyring_service(self, provider: str) -> str:
        """Format scoped OS keyring service identifier."""
        return f"{self._service_namespace}.{provider}"

    def _try_get_keyring_backend(self) -> KeyringBackend | None:
        """Return injected or dynamically imported `KeyringBackend`, or `None`."""
        if self._keyring_backend is not None:
            return self._keyring_backend
        try:
            loaded: KeyringBackend = self._module_loader(KEYRING_MODULE_NAME)
        except ImportError:
            return None
        self._keyring_backend = loaded
        return loaded

    def _require_fallback_allowed(self, provider: str) -> None:
        """Raise `UserTokenStoreError` if keyring is absent and file fallback disabled."""
        if not self._allow_file_fallback:
            raise UserTokenStoreError(
                f"OS keyring is unavailable for '{provider}' and file fallback is disabled."
            )


# endregion


# region Persistence
def _resolve_default_fallback_path(
    fallback_path: Path | None,
    environ: Mapping[str, str] | None,
) -> Path:
    """Resolve fallback token file path from explicit arg, env var, or XDG settings."""
    if fallback_path is not None:
        return fallback_path
    env = os.environ if environ is None else environ
    override = env.get(ENV_TOKEN_STORE_PATH, "").strip()
    if override:
        return Path(override)
    settings = WorktrackSettings.from_env(environ=environ)
    return settings.config_file.parent / DEFAULT_TOKEN_STORE_FILENAME


def _read_fallback_map(
    path: Path,
    remediate_permissions: bool,
) -> dict[str, dict[str, dict[str, object]]]:
    """Verify POSIX `0o600` safety and load fallback token map from `path`."""
    _verify_or_fix_fallback_permissions(path, remediate_permissions)
    if not path.exists():
        return {}
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserTokenStoreError(f"Cannot read local token store '{path.name}'.") from exc

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise UserTokenStoreError(
            f"Local token store '{path.name}' contains invalid JSON.",
            redact=(raw_text,),
        ) from exc

    if not isinstance(parsed, dict):
        raise UserTokenStoreError(
            f"Local token store '{path.name}' root must be a JSON object.",
            redact=(raw_text,),
        )
    return _coerce_store_structure(parsed, raw_text)


def _write_fallback_map_atomic(
    path: Path,
    data: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> None:
    """Atomically write `data` to `path` with strict POSIX `0o600` permissions."""
    if path.is_symlink():
        raise SecretPermissionError(f"Token store path '{path.name}' must not be a symlink.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=TOKEN_DIR_MODE)
    encoded = json.dumps(data, indent=JSON_INDENT_SPACES, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tokens_", suffix=".tmp")
    try:
        os.chmod(tmp_name, TOKEN_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, TOKEN_FILE_MODE)
    except Exception as exc:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise UserTokenStoreError(f"Failed to write local token store '{path.name}'.") from exc


# endregion


# region Validation
def _verify_or_fix_fallback_permissions(path: Path, remediate_permissions: bool) -> None:
    """Reject symlinks and enforce or remediate `0o600` mode on existing fallback file."""
    if path.is_symlink():
        raise SecretPermissionError(f"Token store path '{path.name}' must not be a symlink.")
    if not path.exists():
        return
    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise SecretPermissionError(f"Token store path '{path.name}' must be a regular file.")
    current_mode = stat.S_IMODE(file_stat.st_mode)
    if (current_mode & GROUP_WORLD_PERMISSION_MASK) != 0:
        if remediate_permissions:
            os.chmod(path, TOKEN_FILE_MODE)
            return
        raise SecretPermissionError(
            f"Local token store '{path.name}' has insecure mode {oct(current_mode)}; "
            "expected 0o600."
        )


def _validate_store_keys(provider: str, account: str) -> tuple[str, str]:
    """Validate non-empty `provider` and `account` identifiers."""
    prov = provider.strip()
    acct = account.strip()
    if not prov or not acct:
        raise UserTokenStoreError("provider and account keys must be non-empty strings.")
    return prov, acct


def _serialize_bundle(bundle: OAuthTokenBundle) -> str:
    """Serialize `OAuthTokenBundle` into compact JSON for local workstation storage."""
    payload: dict[str, object] = {
        "access_token": bundle.access_token.expose(),
        "refresh_token": bundle.refresh_token.expose() if bundle.refresh_token else None,
        "expires_in": bundle.expires_in,
        "token_type": bundle.token_type,
        "scope": bundle.scope,
    }
    return json.dumps(payload, sort_keys=True)


def _deserialize_bundle_json(raw_json: str) -> OAuthTokenBundle:
    """Deserialize JSON string from OS keyring into an `OAuthTokenBundle`."""
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise UserTokenStoreError(
            "Stored keyring OAuth token payload is not valid JSON.",
            redact=(raw_json,),
        ) from exc
    if not isinstance(parsed, Mapping):
        raise UserTokenStoreError(
            "Stored keyring OAuth token payload must be a JSON object.",
            redact=(raw_json,),
        )
    return _deserialize_bundle_mapping(parsed)


def _deserialize_bundle_mapping(entry: Mapping[str, object]) -> OAuthTokenBundle:
    """Validate and construct `OAuthTokenBundle` from a parsed dictionary."""
    raw_access = entry.get("access_token")
    if not isinstance(raw_access, str) or not raw_access.strip():
        raise UserTokenStoreError("Stored token entry is missing a valid 'access_token'.")
    raw_refresh = entry.get("refresh_token")
    refresh_val = (
        SecretValue(raw_refresh) if isinstance(raw_refresh, str) and raw_refresh.strip() else None
    )
    raw_expires = entry.get("expires_in")
    expires_in = (
        raw_expires if isinstance(raw_expires, int) and not isinstance(raw_expires, bool) else None
    )
    raw_type = entry.get("token_type")
    token_type = raw_type if isinstance(raw_type, str) and raw_type.strip() else DEFAULT_TOKEN_TYPE
    raw_scope = entry.get("scope")
    scope = raw_scope if isinstance(raw_scope, str) else None
    return OAuthTokenBundle(
        access_token=SecretValue(raw_access),
        refresh_token=refresh_val,
        expires_in=expires_in,
        token_type=token_type,
        scope=scope,
    )


def _coerce_store_structure(
    parsed: Mapping[str, object],
    raw_text: str,
) -> dict[str, dict[str, dict[str, object]]]:
    """Validate nested `{provider: {account: {...}}}` mapping structure."""
    normalized: dict[str, dict[str, dict[str, object]]] = {}
    for prov, accounts in parsed.items():
        if not isinstance(prov, str) or not isinstance(accounts, Mapping):
            raise UserTokenStoreError(
                "Corrupted provider bucket in local token store.",
                redact=(raw_text,),
            )
        normalized[prov] = {}
        for acct, record in accounts.items():
            if not isinstance(acct, str) or not isinstance(record, Mapping):
                raise UserTokenStoreError(
                    "Corrupted account entry in local token store.",
                    redact=(raw_text,),
                )
            normalized[prov][acct] = dict(record)
    return normalized


# endregion
