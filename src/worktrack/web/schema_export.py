"""Deterministic OpenAPI 3.1 schema exporter (`python -m worktrack.web.schema_export`)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TextIO

from worktrack.web.settings import ensure_django_configured

# region Constants
ENV_OPENAPI_SCHEMA_PATH: Final = "WORKTRACK_OPENAPI_SCHEMA_PATH"
DEFAULT_SCHEMA_FILENAME: Final = "openapi.json"
DEFAULT_SCHEMA_OUTPUT_PATH: Final[Path] = Path(__file__).resolve().parent / DEFAULT_SCHEMA_FILENAME
JSON_INDENT_SPACES: Final = 2
EXIT_SUCCESS: Final = 0
EXIT_ERROR: Final = 1
# endregion


# region Validation
def generate_openapi_schema_dict() -> tuple[dict[str, Any], list[str]]:
    """Generate the OpenAPI 3.1 schema dictionary and return `(schema_dict, warnings)`."""
    ensure_django_configured()
    from drf_spectacular.drainage import GENERATOR_STATS
    from drf_spectacular.generators import SchemaGenerator

    from worktrack.web.apps import neutralize_lazy_admin_site

    neutralize_lazy_admin_site()
    GENERATOR_STATS.reset()
    with GENERATOR_STATS.silence():
        generator_cls: Any = SchemaGenerator
        generator = generator_cls()
        schema: dict[str, Any] = generator.get_schema(request=None, public=True)
        captured_warnings = list(getattr(GENERATOR_STATS, "_warn_cache", {}).keys())
        captured_errors = list(getattr(GENERATOR_STATS, "_error_cache", {}).keys())
    return schema, [*captured_errors, *captured_warnings]


def validate_openapi_schema_zero_warnings() -> list[str]:
    """Run `drf-spectacular` generation checks and return any warning/error messages."""
    _schema, diagnostics = generate_openapi_schema_dict()
    return diagnostics


def export_openapi_schema() -> str:
    """Export deterministic (`sort_keys=True`, trailing newline) OpenAPI 3.1 JSON text.

    Raises:
        RuntimeError: If `drf-spectacular` reports any schema errors or warnings.
    """
    schema_dict, diagnostics = generate_openapi_schema_dict()
    if diagnostics:
        joined = "; ".join(diagnostics)
        raise RuntimeError(f"OpenAPI 3.1 schema generation produced warnings/errors: {joined}")
    serialized = json.dumps(schema_dict, indent=JSON_INDENT_SPACES, sort_keys=True)
    return f"{serialized}\n"


# endregion


# region CLI
def resolve_schema_output_path(
    explicit_output: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve output path from `--output`, `WORKTRACK_OPENAPI_SCHEMA_PATH`, or default."""
    if explicit_output and explicit_output.strip():
        return Path(explicit_output.strip())
    env = os.environ if environ is None else environ
    env_override = env.get(ENV_OPENAPI_SCHEMA_PATH, "").strip()
    if env_override:
        return Path(env_override)
    return DEFAULT_SCHEMA_OUTPUT_PATH


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """CLI entrypoint writing deterministic OpenAPI 3.1 JSON to disk."""
    out_stream = sys.stdout if stdout is None else stdout
    err_stream = sys.stderr if stderr is None else stderr
    parser = argparse.ArgumentParser(
        prog="python -m worktrack.web.schema_export",
        description="Export deterministic OpenAPI 3.1 schema for Worktrack.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Target path for generated openapi.json (defaults to src/worktrack/web/openapi.json).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify existing schema file matches freshly generated schema without writing.",
    )
    parsed = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    target_path = resolve_schema_output_path(parsed.output, environ=environ)

    try:
        rendered = export_openapi_schema()
    except Exception as exc:
        err_stream.write(f"error: {exc}\n")
        return EXIT_ERROR

    if parsed.check:
        if not target_path.is_file() or target_path.read_text(encoding="utf-8") != rendered:
            err_stream.write(f"error: OpenAPI schema at {target_path} is out of date.\n")
            return EXIT_ERROR
        out_stream.write(f"OpenAPI 3.1 schema verified: {target_path}\n")
        return EXIT_SUCCESS

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(rendered, encoding="utf-8")
    out_stream.write(f"Exported OpenAPI 3.1 schema to {target_path}\n")
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
# endregion
