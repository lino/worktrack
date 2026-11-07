"""Behavioural and benchmark tests for SecretValue, SecretUri, providers, and TTL cache."""

from __future__ import annotations

import dataclasses
import json
import os
import pickle
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pytest_benchmark.fixture import BenchmarkFixture

from worktrack.secrets import (
    SECRET_REPR,
    AwsSecretsManagerProvider,
    CachingSecretProvider,
    EnvSecretProvider,
    FileSecretProvider,
    KeyringSecretProvider,
    SecretError,
    SecretNotFoundError,
    SecretPermissionError,
    SecretProviderUnavailableError,
    SecretResolutionError,
    SecretResolver,
    SecretUri,
    SecretUriError,
    SecretValue,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
SAMPLE_SECRET_TEXT = "super-secret-oauth-client-value-9876"
ROTATED_SECRET_TEXT = "rotated-corporate-client-secret-4321"
STRICT_FILE_MODE = 0o600
WORLD_READABLE_FILE_MODE = 0o644
TEST_TTL_SECONDS = 60.0
CLOCK_ADVANCE_WITHIN_TTL = 30.0
CLOCK_ADVANCE_PAST_TTL = 61.0
INITIAL_CLOCK_TIME = 1000.0
CACHED_SECRET_P95_BUDGET_SECONDS = 0.001
PERCENTILE_95_FACTOR = 0.95
# endregion


# region Fixtures
class FakeClock:
    """Deterministic monotonic clock test double."""

    def __init__(self, initial: float = INITIAL_CLOCK_TIME) -> None:
        """Initialize fake clock at `initial` seconds."""
        self.now = initial

    def __call__(self) -> float:
        """Return current synthetic monotonic timestamp."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Advance synthetic monotonic timestamp by `seconds`."""
        self.now += seconds


class InMemoryKeyring:
    """In-memory `KeyringBackend` test double."""

    def __init__(self, initial: Mapping[tuple[str, str], str] | None = None) -> None:
        """Initialize keyring store with optional `(service, account)` map."""
        self.entries: dict[tuple[str, str], str] = dict(initial or {})

    def get_password(self, service: str, account: str) -> str | None:
        """Fetch password for `(service, account)` or `None`."""
        return self.entries.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        """Store password for `(service, account)`."""
        self.entries[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        """Remove password for `(service, account)`."""
        self.entries.pop((service, account), None)


class FakeAwsSmClient:
    """In-memory `AwsSecretsManagerClient` test double."""

    def __init__(self, payloads: Mapping[str, Mapping[str, Any]]) -> None:
        """Initialize fake AWS Secrets Manager client with secret responses."""
        self.payloads = dict(payloads)
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, Any]:  # noqa: N803
        """Return configured secret response for `SecretId` or raise error."""
        self.calls.append(SecretId)
        if SecretId not in self.payloads:
            raise KeyError(f"ResourceNotFoundException: {SecretId}")
        return self.payloads[SecretId]


@dataclasses.dataclass(frozen=True, slots=True)
class _SampleHolder:
    """Helper dataclass wrapping a `SecretValue` to test `dataclasses.asdict`."""

    label: str
    secret: SecretValue


def _missing_loader(mod_name: str) -> ModuleType:
    """Simulate an environment where optional dependency `mod_name` is missing."""
    raise ModuleNotFoundError(f"No module named '{mod_name}'")


# endregion


# region Secret Value & URI Tests
def test_secret_value_redacts_repr_str_format_and_is_immutable() -> None:
    """Verify `SecretValue` never leaks plaintext via repr, str, format, or mutation."""
    secret = SecretValue(SAMPLE_SECRET_TEXT)
    assert repr(secret) == SECRET_REPR
    assert str(secret) == SECRET_REPR
    assert f"{secret}" == SECRET_REPR
    assert f"{secret!r}" == SECRET_REPR
    assert secret.expose() == SAMPLE_SECRET_TEXT
    assert secret == SecretValue(SAMPLE_SECRET_TEXT)
    assert secret != SecretValue(ROTATED_SECRET_TEXT)
    assert secret != SAMPLE_SECRET_TEXT

    holder = _SampleHolder(label="jira", secret=secret)
    serialized = str(dataclasses.asdict(holder))
    assert SAMPLE_SECRET_TEXT not in serialized

    with pytest.raises(AttributeError):
        secret._secret = "tampered"
    with pytest.raises(AttributeError):
        delattr(secret, "_secret")
    with pytest.raises(TypeError):
        pickle.dumps(secret)


def test_secret_error_subclasses_scrub_plaintext_and_credential_patterns() -> None:
    """Verify `SecretError` subclasses scrub explicit secrets and bearer/form patterns."""
    secret = SecretValue(SAMPLE_SECRET_TEXT)
    err = SecretResolutionError(
        f"Upstream failed with value={SAMPLE_SECRET_TEXT} and client_secret=inline-leak-123",
        redact=(secret,),
    )
    rendered = str(err)
    assert SAMPLE_SECRET_TEXT not in rendered
    assert "inline-leak-123" not in rendered
    assert "***REDACTED***" in rendered
    assert isinstance(err, SecretError)


def test_secret_uri_parses_valid_schemes_and_rejects_malformed_uris() -> None:
    """Verify `SecretUri.parse` handles env, file, keyring, and aws-sm URIs."""
    env_uri = SecretUri.parse("env://WORKTRACK_JIRA_CLIENT_SECRET")
    assert (env_uri.scheme, env_uri.env_var) == ("env", "WORKTRACK_JIRA_CLIENT_SECRET")

    file_uri = SecretUri.parse("file:///var/run/secrets/jira_client_secret")
    assert file_uri.scheme == "file"
    assert file_uri.file_path == Path("/var/run/secrets/jira_client_secret")

    kr_uri = SecretUri.parse("keyring://worktrack/jira_oauth_client")
    assert (kr_uri.scheme, kr_uri.keyring_service, kr_uri.keyring_account) == (
        "keyring",
        "worktrack",
        "jira_oauth_client",
    )

    sm_plain = SecretUri.parse("aws-sm://prod/worktrack/jira-oauth")
    assert (sm_plain.scheme, sm_plain.secret_id, sm_plain.json_key) == (
        "aws-sm",
        "prod/worktrack/jira-oauth",
        None,
    )

    sm_json = SecretUri.parse("aws-sm://prod/worktrack/jira-oauth#client_secret")
    assert (sm_json.scheme, sm_json.secret_id, sm_json.json_key) == (
        "aws-sm",
        "prod/worktrack/jira-oauth",
        "client_secret",
    )

    invalid_uris = (
        "",
        "not-a-uri",
        "vault://corp/secret",
        "env://123_INVALID",
        "env://VAR/EXTRA",
        "file://relative/path/secret.txt",
        "file:///",
        "keyring://service_only",
        "keyring:///account_only",
        "aws-sm://",
        "aws-sm://prod/secret#",
    )
    for bad_uri in invalid_uris:
        with pytest.raises(SecretUriError):
            SecretUri.parse(bad_uri)


# endregion


# region Provider & Cache Tests
def test_env_and_file_providers_resolve_and_enforce_permissions(tmp_path: Path) -> None:
    """Verify `EnvSecretProvider` and `FileSecretProvider` resolution and mode checks."""
    env_provider = EnvSecretProvider(environ={"JIRA_SECRET": SAMPLE_SECRET_TEXT, "EMPTY": "  "})
    resolved = env_provider.resolve(SecretUri.parse("env://JIRA_SECRET"))
    assert resolved.expose() == SAMPLE_SECRET_TEXT

    with pytest.raises(SecretNotFoundError):
        env_provider.resolve(SecretUri.parse("env://MISSING_SECRET"))
    with pytest.raises(SecretNotFoundError):
        env_provider.resolve(SecretUri.parse("env://EMPTY"))

    secret_file = tmp_path / "jira_secret.txt"
    secret_file.write_text(f"  {SAMPLE_SECRET_TEXT}\n", encoding="utf-8")
    os.chmod(secret_file, WORLD_READABLE_FILE_MODE)

    relaxed_provider = FileSecretProvider(enforce_permissions=False)
    assert relaxed_provider.resolve(SecretUri.parse(secret_file.as_uri())).expose() == (
        SAMPLE_SECRET_TEXT
    )

    strict_provider = FileSecretProvider(enforce_permissions=True)
    with pytest.raises(SecretPermissionError) as exc_info:
        strict_provider.resolve(SecretUri.parse(secret_file.as_uri()))
    assert SAMPLE_SECRET_TEXT not in str(exc_info.value)

    os.chmod(secret_file, STRICT_FILE_MODE)
    assert strict_provider.resolve(SecretUri.parse(secret_file.as_uri())).expose() == (
        SAMPLE_SECRET_TEXT
    )


def test_keyring_and_aws_sm_providers_handle_injection_and_missing_packages() -> None:
    """Verify keyring and AWS Secrets Manager providers resolve or fail cleanly without deps."""
    assert "boto3" not in sys.modules

    unavailable_kr = KeyringSecretProvider(module_loader=_missing_loader)
    with pytest.raises(SecretProviderUnavailableError):
        unavailable_kr.resolve(SecretUri.parse("keyring://worktrack/jira"))

    kr_backend = InMemoryKeyring({("worktrack", "jira"): SAMPLE_SECRET_TEXT})
    kr_provider = KeyringSecretProvider(keyring_backend=kr_backend)
    assert kr_provider.resolve(SecretUri.parse("keyring://worktrack/jira")).expose() == (
        SAMPLE_SECRET_TEXT
    )
    with pytest.raises(SecretNotFoundError):
        kr_provider.resolve(SecretUri.parse("keyring://worktrack/missing"))

    unavailable_sm = AwsSecretsManagerProvider(module_loader=_missing_loader)
    with pytest.raises(SecretProviderUnavailableError, match=r"worktrack\[aws\]"):
        unavailable_sm.resolve(SecretUri.parse("aws-sm://prod/jira"))

    sm_client = FakeAwsSmClient(
        {
            "prod/plain": {"SecretString": SAMPLE_SECRET_TEXT},
            "prod/json": {
                "SecretString": json.dumps({"client_secret": ROTATED_SECRET_TEXT, "other": "x"})
            },
            "prod/bad-json": {"SecretString": f"not-json-{SAMPLE_SECRET_TEXT}"},
        }
    )
    sm_provider = AwsSecretsManagerProvider(client=sm_client)
    assert sm_provider.resolve(SecretUri.parse("aws-sm://prod/plain")).expose() == (
        SAMPLE_SECRET_TEXT
    )
    assert sm_provider.resolve(SecretUri.parse("aws-sm://prod/json#client_secret")).expose() == (
        ROTATED_SECRET_TEXT
    )

    with pytest.raises(SecretNotFoundError) as missing_key_exc:
        sm_provider.resolve(SecretUri.parse("aws-sm://prod/json#missing_key"))
    assert ROTATED_SECRET_TEXT not in str(missing_key_exc.value)

    with pytest.raises(SecretResolutionError) as bad_json_exc:
        sm_provider.resolve(SecretUri.parse("aws-sm://prod/bad-json#client_secret"))
    assert SAMPLE_SECRET_TEXT not in str(bad_json_exc.value)
    assert "boto3" not in sys.modules


def test_dynamic_module_loader_and_secret_binary_support() -> None:
    """Verify lazy `boto3` and `keyring` module loading plus `SecretBinary` decoding."""
    fake_boto3 = ModuleType("boto3")
    sm_client = FakeAwsSmClient({"corp/bin": {"SecretBinary": SAMPLE_SECRET_TEXT.encode("utf-8")}})
    fake_boto3.client = lambda service, **kwargs: sm_client  # type: ignore[attr-defined]

    sm_provider = AwsSecretsManagerProvider(
        region_name="eu-central-1",
        module_loader=lambda name: fake_boto3,
    )
    assert sm_provider.resolve(SecretUri.parse("aws-sm://corp/bin")).expose() == SAMPLE_SECRET_TEXT

    fake_keyring = ModuleType("keyring")
    fake_keyring.get_password = (  # type: ignore[attr-defined]
        lambda service_name, username: ROTATED_SECRET_TEXT
    )
    kr_provider = KeyringSecretProvider(module_loader=lambda name: fake_keyring)
    assert kr_provider.resolve(SecretUri.parse("keyring://worktrack/jira")).expose() == (
        ROTATED_SECRET_TEXT
    )

    env_resolver = SecretResolver.from_env(
        environ={
            "WORKTRACK_SECRET_CACHE_TTL_SECONDS": "15",
            "DYNAMIC_ENV_SECRET": SAMPLE_SECRET_TEXT,
        }
    )
    assert env_resolver.resolve("env://DYNAMIC_ENV_SECRET").expose() == SAMPLE_SECRET_TEXT


def test_secret_resolver_ttl_cache_invalidation_and_telemetry(tmp_path: Path) -> None:
    """Verify TTL cache hits, expiry, rotation eviction, and Prometheus metrics."""
    clock = FakeClock()
    telemetry = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    mutable_env = {"CORP_SECRET": SAMPLE_SECRET_TEXT}
    sm_client = FakeAwsSmClient({"corp/jira": {"SecretString": SAMPLE_SECRET_TEXT}})
    resolver = CachingSecretProvider(
        providers=(EnvSecretProvider(mutable_env), AwsSecretsManagerProvider(client=sm_client)),
        ttl_seconds=TEST_TTL_SECONDS,
        clock=clock,
        telemetry=telemetry,
    )
    assert isinstance(resolver, SecretResolver)
    assert resolver.resolve("aws-sm://corp/jira").expose() == SAMPLE_SECRET_TEXT
    clock.advance(CLOCK_ADVANCE_WITHIN_TTL)
    assert resolver.resolve("aws-sm://corp/jira").expose() == SAMPLE_SECRET_TEXT
    assert len(sm_client.calls) == 1

    sm_client.payloads["corp/jira"] = {"SecretString": ROTATED_SECRET_TEXT}
    assert resolver.invalidate("aws-sm://corp/jira") is True
    assert resolver.invalidate("aws-sm://corp/jira") is False
    assert resolver.resolve("aws-sm://corp/jira").expose() == ROTATED_SECRET_TEXT
    assert len(sm_client.calls) == 2

    assert resolver.resolve("env://CORP_SECRET").expose() == SAMPLE_SECRET_TEXT
    clock.advance(CLOCK_ADVANCE_PAST_TTL)
    mutable_env["CORP_SECRET"] = ROTATED_SECRET_TEXT
    assert resolver.resolve("env://CORP_SECRET").expose() == ROTATED_SECRET_TEXT

    with pytest.raises(SecretUriError):
        resolver.resolve("untrusted_high_cardinality_scheme://foo")

    metrics_text = telemetry.render_metrics_text()
    assert (
        'worktrack_secret_provider_resolves_total{outcome="ok",scheme="aws-sm"} 2.0' in metrics_text
    )
    assert (
        'worktrack_secret_provider_resolves_total{outcome="cache_hit",scheme="aws-sm"} 1.0'
        in metrics_text
    )
    assert (
        'worktrack_secret_provider_resolves_total{outcome="error",scheme="unknown"} 1.0'
        in metrics_text
    )


def test_bench_secret_provider_cached(benchmark: BenchmarkFixture) -> None:
    """Verify cached `SecretResolver.resolve` meets `wt_secret_provider_cached` p95 <= 1ms."""
    resolver = SecretResolver(
        providers=(EnvSecretProvider(environ={"BENCH_SECRET": SAMPLE_SECRET_TEXT}),),
        ttl_seconds=TEST_TTL_SECONDS,
    )
    warmed = resolver.resolve("env://BENCH_SECRET")
    assert warmed.expose() == SAMPLE_SECRET_TEXT

    result = benchmark(resolver.resolve, "env://BENCH_SECRET")
    assert result.expose() == SAMPLE_SECRET_TEXT

    if benchmark.stats is not None:
        sorted_samples = benchmark.stats.stats.sorted_data
        p95_index = min(len(sorted_samples) - 1, int(len(sorted_samples) * PERCENTILE_95_FACTOR))
        p95_seconds = sorted_samples[p95_index]
        assert p95_seconds <= CACHED_SECRET_P95_BUDGET_SECONDS


# endregion
