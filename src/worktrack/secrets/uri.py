"""Parser and validator for `SecretProvider` URI references (`SecretUri`)."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from worktrack.secrets.exceptions import SecretUriError

# region Constants
SCHEME_ENV = "env"
SCHEME_FILE = "file"
SCHEME_KEYRING = "keyring"
SCHEME_AWS_SM = "aws-sm"
SCHEME_UNKNOWN = "unknown"

SUPPORTED_SECRET_SCHEMES: frozenset[str] = frozenset(
    {
        SCHEME_ENV,
        SCHEME_FILE,
        SCHEME_KEYRING,
        SCHEME_AWS_SM,
    }
)

URI_SCHEME_DELIMITER = "://"
FRAGMENT_DELIMITER = "#"
QUERY_DELIMITER = "?"
LOCALHOST_NETLOC = "localhost"
ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# endregion


# region Models
@dataclass(frozen=True, slots=True)
class SecretUri:
    """Validated secret reference across `env`, `file`, `keyring`, and `aws-sm`.

    Examples:
        - `env://WORKTRACK_JIRA_CLIENT_SECRET`
        - `file:///var/run/secrets/jira_client_secret`
        - `keyring://worktrack/jira_client`
        - `aws-sm://prod/worktrack/jira#client_secret`

    Attributes:
        raw: Normalized URI string.
        scheme: Canonical scheme (`env`, `file`, `keyring`, or `aws-sm`).
        env_var: Environment variable identifier for `env://` URIs.
        file_path: Absolute `Path` for `file://` URIs.
        keyring_service: Service namespace for `keyring://` URIs.
        keyring_account: Account/username identifier for `keyring://` URIs.
        secret_id: AWS Secrets Manager secret name or ARN for `aws-sm://` URIs.
        json_key: Optional JSON field key extracted from `#fragment` in `aws-sm://`.
    """

    raw: str
    scheme: str
    env_var: str | None = None
    file_path: Path | None = None
    keyring_service: str | None = None
    keyring_account: str | None = None
    secret_id: str | None = None
    json_key: str | None = None

    @classmethod
    def parse(cls, raw_uri: str) -> SecretUri:
        """Parse and validate a raw secret URI string into a `SecretUri`."""
        stripped = raw_uri.strip()
        if not stripped or URI_SCHEME_DELIMITER not in stripped:
            raise SecretUriError("Secret URI must include '<scheme>://'.")

        scheme_prefix, _, remainder = stripped.partition(URI_SCHEME_DELIMITER)
        scheme = scheme_prefix.lower()
        parser = _SCHEME_PARSERS.get(scheme)
        if parser is None:
            allowed = ", ".join(sorted(SUPPORTED_SECRET_SCHEMES))
            raise SecretUriError(f"Unsupported secret URI scheme '{scheme}' (allowed: {allowed}).")

        normalized = f"{scheme}{URI_SCHEME_DELIMITER}{remainder}"
        return parser(normalized)

    def __str__(self) -> str:
        """Return normalized URI string."""
        return self.raw


# endregion


# region Validation
def extract_safe_metric_scheme(raw_uri: str) -> str:
    """Return bounded Prometheus `scheme` label value for any candidate URI."""
    stripped = raw_uri.strip()
    if URI_SCHEME_DELIMITER not in stripped:
        return SCHEME_UNKNOWN
    candidate = stripped.partition(URI_SCHEME_DELIMITER)[0].lower()
    return candidate if candidate in SUPPORTED_SECRET_SCHEMES else SCHEME_UNKNOWN


def _parse_env_uri(normalized: str) -> SecretUri:
    """Validate an `env://VAR_NAME` secret URI."""
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.path or parsed.query or parsed.fragment:
        raise SecretUriError("env:// URI must not include path, query, or fragment segments.")
    var_name = parsed.netloc
    if not ENV_VAR_NAME_PATTERN.match(var_name):
        raise SecretUriError(f"Invalid environment variable name '{var_name}' in env:// URI.")
    return SecretUri(raw=normalized, scheme=SCHEME_ENV, env_var=var_name)


def _parse_file_uri(normalized: str) -> SecretUri:
    """Validate an absolute `file:///path/to/secret` URI."""
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.query or parsed.fragment:
        raise SecretUriError("file:// URI must not include query or fragment segments.")
    if parsed.netloc and parsed.netloc.lower() != LOCALHOST_NETLOC:
        raise SecretUriError("file:// URI must reference an absolute local path ('file:///...').")
    decoded_path = urllib.parse.unquote(parsed.path)
    if not decoded_path or decoded_path == "/" or decoded_path.endswith("/"):
        raise SecretUriError("file:// URI must point to a file path, not a directory.")
    file_path = Path(decoded_path)
    if not file_path.is_absolute():
        raise SecretUriError("file:// URI path must be absolute.")
    return SecretUri(raw=normalized, scheme=SCHEME_FILE, file_path=file_path)


def _parse_keyring_uri(normalized: str) -> SecretUri:
    """Validate a `keyring://service/account` secret URI."""
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.query or parsed.fragment:
        raise SecretUriError("keyring:// URI must not include query or fragment segments.")
    service = urllib.parse.unquote(parsed.netloc).strip()
    account = urllib.parse.unquote(parsed.path.lstrip("/")).strip()
    if not service or not account:
        raise SecretUriError("keyring:// URI must match 'keyring://<service>/<account>'.")
    return SecretUri(
        raw=normalized,
        scheme=SCHEME_KEYRING,
        keyring_service=service,
        keyring_account=account,
    )


def _parse_aws_sm_uri(normalized: str) -> SecretUri:
    """Validate an `aws-sm://secret-id` or `aws-sm://secret-id#json_key` URI."""
    remainder = normalized.partition(URI_SCHEME_DELIMITER)[2]
    if QUERY_DELIMITER in remainder:
        raise SecretUriError("aws-sm:// URI must not include query parameters.")

    json_key: str | None = None
    if FRAGMENT_DELIMITER in remainder:
        raw_id, _, raw_key = remainder.partition(FRAGMENT_DELIMITER)
        decoded_key = urllib.parse.unquote(raw_key).strip()
        if not decoded_key or FRAGMENT_DELIMITER in decoded_key:
            raise SecretUriError("aws-sm:// URI '#json_key' fragment must be non-empty.")
        json_key = decoded_key
    else:
        raw_id = remainder

    secret_id = urllib.parse.unquote(raw_id).strip()
    if (
        not secret_id
        or secret_id.startswith("/")
        or secret_id.endswith("/")
        or any(ch.isspace() for ch in secret_id)
    ):
        raise SecretUriError("aws-sm:// URI must specify a valid non-empty secret-id.")
    return SecretUri(
        raw=normalized,
        scheme=SCHEME_AWS_SM,
        secret_id=secret_id,
        json_key=json_key,
    )


_SCHEME_PARSERS: dict[str, Callable[[str], SecretUri]] = {
    SCHEME_ENV: _parse_env_uri,
    SCHEME_FILE: _parse_file_uri,
    SCHEME_KEYRING: _parse_keyring_uri,
    SCHEME_AWS_SM: _parse_aws_sm_uri,
}
# endregion
