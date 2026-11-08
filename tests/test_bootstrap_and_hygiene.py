"""Behavioural bootstrap, telemetry, and craft hygiene test suite for Worktrack."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from worktrack.config import DEFAULT_LOG_LEVEL, ConfigurationError, WorktrackSettings
from worktrack.telemetry import (
    ALLOWED_METRIC_LABELS,
    CANONICAL_METRIC_NAMES,
    DEFAULT_MISSING_LABEL_VALUE,
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    MetricsHttpServer,
    StructuredEventLogger,
    TelemetryCardinalityError,
    TelemetryRecorder,
    resolve_trace_id,
)

# region Constants
REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOW_FILE_LENGTH_PATTERN = re.compile(
    r"#\s*fritz:\s*allow-file-length=(\d+)\s+reason=\"([^\"]+)\""
)
REGION_OPEN_PATTERN = re.compile(r"^\s*#\s*region\s+(.+?)\s*$")
REGION_CLOSE_PATTERN = re.compile(r"^\s*#\s*endregion\b")
HTTP_OK_STATUS = 200
HTTP_NOT_FOUND_STATUS = 404
CUSTOM_TEST_PORT = 19112
CUSTOM_SHUTDOWN_TIMEOUT = 2.5
INVALID_HIGH_PORT = 70000
SAMPLE_IPC_LATENCY_FIRST = 0.004
SAMPLE_IPC_LATENCY_SECOND = 0.008
EXPECTED_TWO_RUNS = 2.0
# endregion


# region Fixtures
@pytest.fixture
def isolated_home(tmp_path: Path) -> Path:
    """Create an isolated temporary home directory for configuration tests."""
    home = tmp_path / "synthetic_home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _load_hygiene_limits() -> dict[str, int]:
    """Load craft hygiene thresholds from workspace `.fritz.toml`."""
    parsed = tomllib.loads((REPO_ROOT / ".fritz.toml").read_text(encoding="utf-8"))
    hygiene = parsed["hygiene"]
    return {
        "max_file_lines": int(hygiene["max_file_lines"]),
        "max_function_lines": int(hygiene["max_function_lines"]),
        "max_complexity": int(hygiene["max_cyclomatic_complexity"]),
        "regions_threshold": int(hygiene["regions_section_threshold"]),
    }


def _iter_python_files() -> list[Path]:
    """Collect all Python source and test files under `src/` and `tests/`."""
    files: list[Path] = []
    for folder_name in ("src", "tests"):
        folder = REPO_ROOT / folder_name
        if folder.exists():
            files.extend(sorted(folder.rglob("*.py")))
    return files


def _compute_mccabe_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Compute McCabe cyclomatic complexity for an AST function node."""
    branch_types = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ExceptHandler,
        ast.IfExp,
        ast.comprehension,
        ast.match_case,
    )
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, branch_types):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1
    return complexity


def _count_statement_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count non-docstring executable statement lines inside a function node."""
    has_doc = ast.get_docstring(node, clean=False) is not None
    statements = node.body[1:] if has_doc else node.body
    if not statements:
        return 0
    first_line = statements[0].lineno
    last_line = max(getattr(stmt, "end_lineno", stmt.lineno) for stmt in statements)
    return max(0, last_line - first_line + 1)


# endregion


# region Configuration Tests
def test_settings_layered_xdg_toml_and_env_precedence(tmp_path: Path, isolated_home: Path) -> None:
    """Verify layered XDG defaults, TOML config file, and env overrides."""
    xdg_cfg, xdg_data, xdg_run = tmp_path / "xdg_cfg", tmp_path / "xdg_data", tmp_path / "xdg_run"
    base_env = {
        "XDG_CONFIG_HOME": str(xdg_cfg),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_RUNTIME_DIR": str(xdg_run),
    }
    defaults = WorktrackSettings.from_env(environ=base_env, home_dir=isolated_home)
    assert defaults.config_file == xdg_cfg / "worktrack" / "config.toml"
    assert defaults.db_path == xdg_data / "worktrack" / "ledger.sqlite3"
    assert defaults.socket_path == xdg_run / "worktrack" / "worktrackd.sock"
    assert defaults.metrics_shutdown_timeout == DEFAULT_SHUTDOWN_TIMEOUT_SECONDS

    custom_cfg = tmp_path / "custom" / "worktrack.toml"
    custom_cfg.parent.mkdir(parents=True, exist_ok=True)
    toml_db, toml_sock = tmp_path / "toml_db" / "node.sqlite3", tmp_path / "toml_run" / "wt.sock"
    custom_cfg.write_text(
        f'[worktrack]\ndb_path = "{toml_db.as_posix()}"\n'
        f'socket_path = "{toml_sock.as_posix()}"\nmetrics_port = {CUSTOM_TEST_PORT}\n',
        encoding="utf-8",
    )
    from_file = WorktrackSettings.from_env(
        environ={**base_env, "WORKTRACK_CONFIG_FILE": str(custom_cfg)}, home_dir=isolated_home
    )
    assert from_file.config_file == custom_cfg
    assert (from_file.db_path, from_file.socket_path) == (toml_db, toml_sock)
    assert from_file.metrics_port == CUSTOM_TEST_PORT

    env_db, env_sock = tmp_path / "env" / "override.sqlite3", tmp_path / "env" / "override.sock"
    env_port = CUSTOM_TEST_PORT + 1
    overridden = WorktrackSettings.from_env(
        environ={
            **base_env,
            "WORKTRACK_CONFIG_FILE": str(custom_cfg),
            "WORKTRACK_DB_PATH": str(env_db),
            "WORKTRACK_SOCKET_PATH": str(env_sock),
            "WORKTRACK_METRICS_PORT": str(env_port),
            "WORKTRACK_METRICS_SHUTDOWN_TIMEOUT": str(CUSTOM_SHUTDOWN_TIMEOUT),
        },
        home_dir=isolated_home,
    )
    assert (overridden.db_path, overridden.socket_path) == (env_db, env_sock)
    assert overridden.metrics_port == env_port
    assert overridden.metrics_shutdown_timeout == CUSTOM_SHUTDOWN_TIMEOUT


def test_settings_rejects_invalid_ports_and_modes(isolated_home: Path) -> None:
    """Verify WorktrackSettings validates port bounds, timeouts, and modes."""
    invalid_envs = (
        {"WORKTRACK_METRICS_PORT": str(INVALID_HIGH_PORT)},
        {"WORKTRACK_METRICS_PORT": "not-an-int"},
        {"WORKTRACK_METRICS_SHUTDOWN_TIMEOUT": "0"},
        {"WORKTRACK_MODE": "surveillance"},
    )
    for invalid_env in invalid_envs:
        with pytest.raises(ConfigurationError):
            WorktrackSettings.from_env(environ=invalid_env, home_dir=isolated_home)


def test_repository_contains_zero_hardcoded_user_paths() -> None:
    """Verify tracked files contain zero literal OS user home prefixes."""
    forbidden_prefixes = (
        "/" + "Users" + "/",
        "/" + "home" + "/",
        "~" + "/",
        "C:\\" + "Users" + "\\",
    )
    completed = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    )
    tracked_rel_paths = [line for line in completed.stdout.splitlines() if line.strip()]
    root_files = (
        "pyproject.toml",
        "Dockerfile",
        "compose.yaml",
        "zensical.toml",
        ".github/workflows/ci.yml",
    )
    untracked_targets = [
        *(REPO_ROOT / rel for rel in root_files),
        *_iter_python_files(),
        *sorted((REPO_ROOT / "docs").rglob("*.md")),
    ]
    candidates = {REPO_ROOT / rel for rel in tracked_rel_paths} | set(untracked_targets)
    violations: list[str] = []
    for file_path in sorted(candidates):
        if not file_path.is_file() or file_path.name == "uv.lock":
            continue
        content = file_path.read_text(encoding="utf-8")
        for prefix in forbidden_prefixes:
            if prefix in content:
                violations.append(f"{file_path.relative_to(REPO_ROOT)}: {prefix}")
    assert violations == []


# endregion


# region Telemetry Tests
def test_telemetry_persists_counters_and_histograms_across_cli_runs(tmp_path: Path) -> None:
    """Verify short-lived CLI runs persist counters and histograms atomically."""
    telemetry_dir = tmp_path / "telemetry"
    assert {
        "worktrack_attribution_queries_total",
        "worktrack_context_facts_asserted_total",
        "worktrack_ipc_active_connections",
        "worktrack_agenda_items_scheduled_total",
        "worktrack_source_syncs_total",
        "worktrack_http_requests_total",
        "worktrack_http_request_duration_seconds",
    } <= set(CANONICAL_METRIC_NAMES)

    first_run = TelemetryRecorder(telemetry_dir=telemetry_dir)
    first_run.record_cli_command(subcommand="status", outcome="ok")
    first_run.record_interval(mode="structured")
    first_run.increment_counter("worktrack_attribution_queries_total")
    first_run.observe_ipc_duration(
        method="status", outcome="ok", duration_seconds=SAMPLE_IPC_LATENCY_FIRST
    )

    second_run = TelemetryRecorder(telemetry_dir=telemetry_dir)
    second_run.record_cli_command(subcommand="status", outcome="ok")
    second_run.observe_ipc_duration(
        method="status", outcome="ok", duration_seconds=SAMPLE_IPC_LATENCY_SECOND
    )

    exposition = second_run.render_metrics_text()
    for metric_name in CANONICAL_METRIC_NAMES:
        assert metric_name in exposition
    assert (
        f'worktrack_cli_commands_total{{outcome="ok",subcommand="status"}} {EXPECTED_TWO_RUNS}'
    ) in exposition
    assert 'worktrack_intervals_recorded_total{mode="structured"} 1.0' in exposition
    assert "worktrack_attribution_queries_total 1.0" in exposition
    assert (
        f'worktrack_ipc_request_duration_seconds_count{{method="status",outcome="ok"}} '
        f"{EXPECTED_TWO_RUNS}"
    ) in exposition


def test_telemetry_rejects_unknown_labels_and_defaults_omitted_labels(tmp_path: Path) -> None:
    """Verify high-cardinality keys fail fast while omitted labels default cleanly."""
    recorder = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry", max_label_values=3)
    assert "email" not in ALLOWED_METRIC_LABELS
    assert "jira_key" not in ALLOWED_METRIC_LABELS

    with pytest.raises(TelemetryCardinalityError):
        recorder.increment_counter(
            "worktrack_cli_commands_total",
            {"subcommand": "status", "outcome": "ok", "email": "alice@example.org"},
        )
    with pytest.raises(TelemetryCardinalityError):
        recorder.increment_counter(
            "worktrack_intervals_recorded_total", {"mode": "structured", "source": "cli"}
        )
    for forbidden_value in (
        "PROJ-1234",
        "alice@example.org",
        "550e8400-e29b-41d4-a716-446655440000",
    ):
        with pytest.raises(TelemetryCardinalityError):
            recorder.record_cli_command(subcommand=forbidden_value, outcome="ok")

    recorder.increment_counter("worktrack_egress_bypass_blocked_total", {})
    recorder.observe_histogram(
        "worktrack_rollup_compute_duration_seconds", {}, SAMPLE_IPC_LATENCY_FIRST
    )
    rendered = recorder.render_metrics_text()
    default_lbl = DEFAULT_MISSING_LABEL_VALUE
    assert (
        f'worktrack_egress_bypass_blocked_total{{sink="{default_lbl}",source="{default_lbl}"}} 1.0'
        in rendered
    )
    assert (
        f'worktrack_rollup_compute_duration_seconds_count{{cadence="{default_lbl}",'
        f'outcome="{default_lbl}"}} 1.0'
    ) in rendered

    for cmd in ("in", "out", "status"):
        recorder.record_cli_command(subcommand=cmd, outcome="ok")
    with pytest.raises(TelemetryCardinalityError):
        recorder.record_cli_command(subcommand="fourth", outcome="ok")


def test_structured_jsonl_logger_propagates_fritz_trace_id(tmp_path: Path) -> None:
    """Verify FRITZ_TRACE_ID propagates into structured JSONL telemetry events."""
    telemetry_dir = tmp_path / "telemetry"
    logger = StructuredEventLogger(telemetry_dir=telemetry_dir)
    expected_trace = "4bf92f3577b34da6a3ce929d0e0e4736"

    emitted = logger.emit(
        "cli.command.completed",
        environ={"FRITZ_TRACE_ID": expected_trace},
        subcommand="status",
        outcome="ok",
    )
    assert emitted["trace_id"] == expected_trace
    assert emitted["level"] == DEFAULT_LOG_LEVEL
    assert len(resolve_trace_id(environ={})) == 32

    lines = (telemetry_dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["trace_id"] == expected_trace
    assert parsed["event"] == "cli.command.completed"
    assert parsed["attributes"]["subcommand"] == "status"


def test_metrics_http_server_exposes_metrics_and_healthz(tmp_path: Path) -> None:
    """Verify HTTP server responds on `/metrics`, `/healthz`, and 404 elsewhere."""
    recorder = TelemetryRecorder(telemetry_dir=tmp_path / "telemetry")
    recorder.record_outbox_flush(sink="jira", cadence="eod", outcome="ok")

    server = MetricsHttpServer(
        recorder=recorder,
        host="127.0.0.1",
        port=0,
        shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    )
    with server:
        with urllib.request.urlopen(f"{server.base_url}/healthz") as health_resp:
            assert health_resp.status == HTTP_OK_STATUS
            payload = json.loads(health_resp.read().decode("utf-8"))
            assert (payload["status"], payload["namespace"]) == ("ok", "worktrack")

        with urllib.request.urlopen(f"{server.base_url}/metrics") as metrics_resp:
            assert metrics_resp.status == HTTP_OK_STATUS
            body = metrics_resp.read().decode("utf-8")
            assert (
                'worktrack_outbox_flushes_total{cadence="eod",outcome="ok",sink="jira"} 1.0' in body
            )

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{server.base_url}/unknown")
        try:
            assert exc_info.value.code == HTTP_NOT_FOUND_STATUS
        finally:
            exc_info.value.close()


# endregion


# region Hygiene Tests
def test_ast_craft_hygiene_docstrings_complexity_and_length() -> None:
    """Verify AST function length, McCabe complexity, file length, and pydoc."""
    limits = _load_hygiene_limits()
    py_files = _iter_python_files()
    assert py_files, "Expected Python modules in src/ and tests/"

    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        lines = source.splitlines()
        override_match = ALLOW_FILE_LENGTH_PATTERN.search(source)
        allowed_lines = int(override_match.group(1)) if override_match else limits["max_file_lines"]
        assert len(lines) <= allowed_lines, f"{py_file.name} has {len(lines)} > {allowed_lines}"

        tree = ast.parse(source, filename=str(py_file))
        assert ast.get_docstring(tree), f"Missing module docstring in {py_file.name}"

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert ast.get_docstring(node), (
                    f"Missing class docstring on {py_file.name}:{node.name}"
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert ast.get_docstring(node), f"Missing docstring on {py_file.name}:{node.name}"
                stmt_lines = _count_statement_lines(node)
                assert stmt_lines <= limits["max_function_lines"], (
                    f"{py_file.name}:{node.name} has {stmt_lines} statement lines"
                )
                complexity = _compute_mccabe_complexity(node)
                assert complexity <= limits["max_complexity"], (
                    f"{py_file.name}:{node.name} has McCabe complexity {complexity}"
                )


def test_regions_comments_are_balanced_and_synced_with_registry() -> None:
    """Verify `# region` blocks balance and stay synchronized with `.regions.md`."""
    registry_path = REPO_ROOT / ".regions.md"
    registered_names: set[str] | None = None
    if registry_path.exists():
        registered_names = set(re.findall(r"`([^`]+)`", registry_path.read_text(encoding="utf-8")))

    for py_file in _iter_python_files():
        open_stack: list[str] = []
        used_regions: list[str] = []
        for line in py_file.read_text(encoding="utf-8").splitlines():
            open_match = REGION_OPEN_PATTERN.match(line)
            if open_match:
                open_stack.append(open_match.group(1))
                used_regions.append(open_match.group(1))
            elif REGION_CLOSE_PATTERN.match(line):
                assert open_stack, f"Unmatched # endregion in {py_file.name}"
                open_stack.pop()
        assert not open_stack, f"Unclosed # region {open_stack} in {py_file.name}"
        if registered_names is not None:
            for region_name in used_regions:
                assert region_name in registered_names, (
                    f"Region '{region_name}' in {py_file.name} missing from .regions.md"
                )


def test_runtime_ci_and_zensical_skeleton_present() -> None:
    """Verify Docker, CI workflow, and Zensical user manual exist."""
    required_rels = (
        "Dockerfile",
        "compose.yaml",
        ".github/workflows/ci.yml",
        "zensical.toml",
        "docs/index.md",
        "docs/user/guide.md",
    )
    for rel in required_rels:
        assert (REPO_ROOT / rel).is_file(), f"Missing required bootstrap file: {rel}"

    zensical_cfg = tomllib.loads((REPO_ROOT / "zensical.toml").read_text(encoding="utf-8"))
    nav_serialized = json.dumps(zensical_cfg["project"]["nav"])
    assert "user/guide.md" in nav_serialized
    assert "operations/runbook.md" not in nav_serialized


# endregion
