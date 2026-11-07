"""Optional AWS Secrets Manager provider (`worktrack[aws]`) without hard `boto3` dependency."""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from worktrack.secrets.exceptions import (
    SecretNotFoundError,
    SecretProviderUnavailableError,
    SecretResolutionError,
    SecretUriError,
    SecretValue,
)
from worktrack.secrets.uri import SCHEME_AWS_SM, SecretUri

# region Constants
BOTO3_MODULE_NAME = "boto3"
AWS_SECRETSMANAGER_SERVICE = "secretsmanager"
ENV_WORKTRACK_AWS_REGION = "WORKTRACK_AWS_REGION"
ENV_AWS_REGION = "AWS_REGION"
AWS_NOT_FOUND_MARKERS: tuple[str, ...] = ("ResourceNotFoundException", "SecretNotFound")
# endregion


# region Protocols
class AwsSecretsManagerClient(Protocol):
    """Structural protocol for `boto3` Secrets Manager client objects."""

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, Any]:  # noqa: N803
        """Fetch a secret payload dictionary for `SecretId`."""
        ...


# endregion


# region Providers
class AwsSecretsManagerProvider:
    """Resolve `aws-sm://secret-id[#json_key]` via injectable client or lazy `boto3`."""

    def __init__(
        self,
        *,
        client: AwsSecretsManagerClient | None = None,
        region_name: str | None = None,
        environ: Mapping[str, str] | None = None,
        module_loader: Callable[[str], Any] = importlib.import_module,
    ) -> None:
        """Initialize AWS Secrets Manager provider.

        Args:
            client: Optional pre-configured Secrets Manager client test/runtime double.
            region_name: Optional AWS region override when lazily constructing `boto3`.
            environ: Optional environment mapping consulted for region fallback.
            module_loader: Dynamic module loader defaulting to `importlib.import_module`.
        """
        self._client = client
        self._region_name = region_name
        self._environ = environ
        self._module_loader = module_loader

    @property
    def scheme(self) -> str:
        """Return canonical URI scheme (`aws-sm`)."""
        return SCHEME_AWS_SM

    def resolve(self, uri: SecretUri) -> SecretValue:
        """Resolve `aws-sm://` plain or JSON-keyed secret into a `SecretValue`."""
        if uri.scheme != SCHEME_AWS_SM or not uri.secret_id:
            raise SecretUriError("AwsSecretsManagerProvider requires an 'aws-sm://' URI.")

        client = self._get_or_create_client()
        response = _fetch_secret_payload(client, uri.secret_id)
        raw_secret = _extract_raw_secret_text(response, uri.secret_id)
        if uri.json_key is None:
            return SecretValue(raw_secret)
        return SecretValue(_extract_json_secret_field(raw_secret, uri.secret_id, uri.json_key))

    def _get_or_create_client(self) -> AwsSecretsManagerClient:
        """Return injected client or lazily construct a `boto3` Secrets Manager client."""
        if self._client is not None:
            return self._client
        try:
            boto3_mod = self._module_loader(BOTO3_MODULE_NAME)
        except ImportError as exc:
            raise SecretProviderUnavailableError(
                "AWS Secrets Manager support requires 'boto3'; install 'worktrack[aws]'."
            ) from exc

        region = self._resolve_region()
        kwargs: dict[str, str] = {"region_name": region} if region else {}
        created: AwsSecretsManagerClient = boto3_mod.client(AWS_SECRETSMANAGER_SERVICE, **kwargs)
        self._client = created
        return created

    def _resolve_region(self) -> str | None:
        """Resolve optional AWS region from explicit setting or environment."""
        if self._region_name and self._region_name.strip():
            return self._region_name.strip()
        env = os.environ if self._environ is None else self._environ
        for key in (ENV_WORKTRACK_AWS_REGION, ENV_AWS_REGION):
            candidate = env.get(key, "").strip()
            if candidate:
                return candidate
        return None


# endregion


# region Resolution
def _fetch_secret_payload(
    client: AwsSecretsManagerClient,
    secret_id: str,
) -> Mapping[str, Any]:
    """Call `get_secret_value` and classify missing vs operational failures."""
    try:
        return client.get_secret_value(SecretId=secret_id)
    except SecretResolutionError:
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if any(marker in detail for marker in AWS_NOT_FOUND_MARKERS):
            raise SecretNotFoundError(
                f"AWS Secrets Manager secret '{secret_id}' was not found."
            ) from exc
        raise SecretResolutionError(
            f"Failed to retrieve AWS Secrets Manager secret '{secret_id}'."
        ) from exc


def _extract_raw_secret_text(response: Mapping[str, Any], secret_id: str) -> str:
    """Extract non-empty UTF-8 string from `SecretString` or `SecretBinary`."""
    secret_string = response.get("SecretString")
    if isinstance(secret_string, str):
        if not secret_string.strip():
            raise SecretNotFoundError(f"AWS secret '{secret_id}' has an empty SecretString.")
        return secret_string

    secret_binary = response.get("SecretBinary")
    if isinstance(secret_binary, (bytes, bytearray)):
        try:
            decoded = bytes(secret_binary).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretResolutionError(
                f"AWS secret '{secret_id}' SecretBinary is not valid UTF-8."
            ) from exc
        if not decoded.strip():
            raise SecretNotFoundError(f"AWS secret '{secret_id}' has an empty SecretBinary.")
        return decoded

    raise SecretNotFoundError(f"AWS secret '{secret_id}' contained neither SecretString nor bytes.")


def _extract_json_secret_field(raw_secret: str, secret_id: str, json_key: str) -> str:
    """Parse JSON secret payload and return string value for `json_key` safely."""
    try:
        parsed = json.loads(raw_secret)
    except json.JSONDecodeError as exc:
        raise SecretResolutionError(
            f"AWS secret '{secret_id}' is not valid JSON for fragment '#{json_key}'.",
            redact=(raw_secret,),
        ) from exc

    if not isinstance(parsed, Mapping) or json_key not in parsed:
        raise SecretNotFoundError(
            f"JSON key '{json_key}' not found in AWS secret '{secret_id}'.",
            redact=(raw_secret,),
        )
    value = parsed[json_key]
    if not isinstance(value, str) or not value.strip():
        raise SecretResolutionError(
            f"JSON key '{json_key}' in AWS secret '{secret_id}' must be a non-empty string.",
            redact=(raw_secret,),
        )
    return value


# endregion
