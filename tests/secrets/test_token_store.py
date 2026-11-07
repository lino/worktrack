"""Behavioural tests for workstation-local `UserTokenStore` keyring and `0o600` fallback."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

from worktrack.config import WorktrackSettings
from worktrack.secrets import (
    OAuthTokenBundle,
    SecretPermissionError,
    SecretValue,
    UserTokenStore,
    UserTokenStoreError,
)

# region Constants
SAMPLE_ACCESS_TOKEN = "user-local-access-token-777"
SAMPLE_REFRESH_TOKEN = "user-local-refresh-token-888"
SECOND_ACCESS_TOKEN = "second-account-access-token-999"
EXPECTED_TOKEN_FILE_MODE = 0o600
INSECURE_GROUP_READABLE_MODE = 0o640
SAMPLE_EXPIRES_IN = 7200
# endregion


# region Fixtures
class FakeWorkstationKeyring:
    """In-memory workstation `KeyringBackend` test double."""

    def __init__(self) -> None:
        """Initialize empty in-memory credential dictionary."""
        self.credentials: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        """Return stored password payload or `None`."""
        return self.credentials.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        """Persist password payload in memory."""
        self.credentials[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        """Delete password payload from memory."""
        self.credentials.pop((service, account), None)


def _missing_keyring_loader(module_name: str) -> ModuleType:
    """Simulate an environment where the `keyring` package is absent."""
    raise ModuleNotFoundError(f"No module named '{module_name}'")


def _sample_bundle(access_raw: str = SAMPLE_ACCESS_TOKEN) -> OAuthTokenBundle:
    """Build a sample `OAuthTokenBundle` for local token store tests."""
    return OAuthTokenBundle(
        access_token=SecretValue(access_raw),
        refresh_token=SecretValue(SAMPLE_REFRESH_TOKEN),
        expires_in=SAMPLE_EXPIRES_IN,
        token_type="Bearer",
        scope="read:jira-work write:jira-work",
    )


# endregion


# region User Token Store Tests
def test_user_token_store_persists_in_keyring_without_touching_disk(tmp_path: Path) -> None:
    """Verify `UserTokenStore` uses `KeyringBackend` when available and skips disk."""
    keyring = FakeWorkstationKeyring()
    fallback_file = tmp_path / "tokens.json"
    store = UserTokenStore(keyring_backend=keyring, fallback_path=fallback_file)

    store.store("jira", _sample_bundle())
    store.store("jira", _sample_bundle(SECOND_ACCESS_TOKEN), account="secondary")
    assert not fallback_file.exists()

    loaded_default = store.load("jira")
    loaded_secondary = store.load("jira", account="secondary")
    assert loaded_default is not None
    assert loaded_default.access_token.expose() == SAMPLE_ACCESS_TOKEN
    assert loaded_default.refresh_token is not None
    assert loaded_default.refresh_token.expose() == SAMPLE_REFRESH_TOKEN
    assert loaded_secondary is not None
    assert loaded_secondary.access_token.expose() == SECOND_ACCESS_TOKEN

    assert store.delete("jira") is True
    assert store.delete("jira") is False
    assert store.load("jira") is None
    assert store.load("jira", account="secondary") is not None


def test_user_token_store_headless_0o600_fallback_and_permission_guards(tmp_path: Path) -> None:
    """Verify POSIX `0o600` atomic file fallback, rejection, and permission remediation."""
    settings = WorktrackSettings.from_env(
        environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg_cfg")},
        home_dir=tmp_path / "synthetic_home",
    )
    store = UserTokenStore.from_settings(settings, module_loader=_missing_keyring_loader)

    store.store("jira", _sample_bundle())
    fallback_file = store.fallback_path
    assert fallback_file.is_file()
    assert stat.S_IMODE(fallback_file.stat().st_mode) == EXPECTED_TOKEN_FILE_MODE

    loaded = store.load("jira")
    assert loaded is not None
    assert loaded.access_token.expose() == SAMPLE_ACCESS_TOKEN

    os.chmod(fallback_file, INSECURE_GROUP_READABLE_MODE)
    with pytest.raises(SecretPermissionError):
        store.load("jira")

    remediating_store = UserTokenStore(
        fallback_path=fallback_file,
        remediate_permissions=True,
        module_loader=_missing_keyring_loader,
    )
    recovered = remediating_store.load("jira")
    assert recovered is not None
    assert recovered.access_token.expose() == SAMPLE_ACCESS_TOKEN
    assert stat.S_IMODE(fallback_file.stat().st_mode) == EXPECTED_TOKEN_FILE_MODE

    assert remediating_store.delete("jira") is True
    assert remediating_store.load("jira") is None


def test_user_token_store_rejects_symlinks_and_scrubs_corrupt_json(tmp_path: Path) -> None:
    """Verify symlinked fallback files are rejected and corrupt JSON never leaks secrets."""
    real_file = tmp_path / "real_tokens.json"
    real_file.write_text(f"corrupt-payload-{SAMPLE_ACCESS_TOKEN}", encoding="utf-8")
    os.chmod(real_file, EXPECTED_TOKEN_FILE_MODE)

    corrupt_store = UserTokenStore(
        fallback_path=real_file,
        module_loader=_missing_keyring_loader,
    )
    with pytest.raises(UserTokenStoreError) as corrupt_exc:
        corrupt_store.load("jira")
    assert SAMPLE_ACCESS_TOKEN not in str(corrupt_exc.value)

    symlink_file = tmp_path / "symlink_tokens.json"
    symlink_file.symlink_to(real_file)
    symlink_store = UserTokenStore(
        fallback_path=symlink_file,
        module_loader=_missing_keyring_loader,
    )
    with pytest.raises(SecretPermissionError):
        symlink_store.load("jira")


def test_user_token_store_falls_back_when_keyring_raises_runtime_error(tmp_path: Path) -> None:
    """Verify headless runtime keyring failures fall back to `0o600` file or fail when disabled."""
    broken_keyring = ModuleType("keyring")

    def _raise_headless(*args: object) -> None:
        """Simulate a headless Linux session without an active SecretService D-Bus."""
        raise RuntimeError("No recommended keyring backend available")

    broken_keyring.get_password = _raise_headless  # type: ignore[attr-defined]
    broken_keyring.set_password = _raise_headless  # type: ignore[attr-defined]
    broken_keyring.delete_password = _raise_headless  # type: ignore[attr-defined]

    fallback_file = tmp_path / "headless_tokens.json"
    fallback_store = UserTokenStore(
        fallback_path=fallback_file,
        module_loader=lambda _: broken_keyring,
    )
    fallback_store.store("jira", _sample_bundle())
    assert stat.S_IMODE(fallback_file.stat().st_mode) == EXPECTED_TOKEN_FILE_MODE
    loaded = fallback_store.load("jira")
    assert loaded is not None and loaded.access_token.expose() == SAMPLE_ACCESS_TOKEN

    strict_store = UserTokenStore(
        fallback_path=fallback_file,
        allow_file_fallback=False,
        module_loader=lambda _: broken_keyring,
    )
    with pytest.raises(UserTokenStoreError) as store_exc:
        strict_store.store("jira", _sample_bundle())
    assert SAMPLE_ACCESS_TOKEN not in str(store_exc.value)


# endregion
