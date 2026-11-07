"""Behavioural tests for `shell/` prompt plugins and `wt_ipc_status_warm` benchmark."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest_benchmark.fixture import BenchmarkFixture

from worktrack.ipc import (
    DaemonStatusSnapshot,
    IpcClient,
    IpcEndpointConfig,
    StaticStatusProvider,
    WorktrackIpcServer,
)
from worktrack.telemetry import TelemetryRecorder

# region Constants
REPO_ROOT = Path(__file__).resolve().parents[2]
SHELL_DIR = REPO_ROOT / "shell"
STARSHIP_CONFIG_PATH = SHELL_DIR / "starship.toml"
ZSH_PLUGIN_PATH = SHELL_DIR / "worktrack.plugin.zsh"
POWERSHELL_MODULE_PATH = SHELL_DIR / "WorktrackPrompt.psm1"
SHELL_README_PATH = SHELL_DIR / "README.md"

ENV_SHORT_TMPDIR = "WORKTRACK_TEST_SHORT_TMPDIR"
MAX_SHORT_TMPDIR_LEN = 32
STUB_SCRIPT_MODE = 0o755
WARM_IPC_P95_BUDGET_SECONDS = 0.010
PERCENTILE_95_FRACTION = 0.95
SAMPLE_WARM_ELAPSED = 1800
SAMPLE_WARM_DAILY = 14400
# endregion


# region Fixtures
def _resolve_short_socket_base() -> Path:
    """Resolve a short temporary directory path suitable for `AF_UNIX` bind limits."""
    override = os.environ.get(ENV_SHORT_TMPDIR, "").strip()
    if override:
        return Path(override)
    default_tmp = Path(tempfile.gettempdir())
    if len(str(default_tmp)) <= MAX_SHORT_TMPDIR_LEN:
        return default_tmp
    posix_short = Path(os.sep, "tmp")
    return posix_short if posix_short.is_dir() else default_tmp


@pytest.fixture
def short_sock_dir() -> Iterator[Path]:
    """Yield a short temporary runtime directory cleaned up after the test."""
    with tempfile.TemporaryDirectory(prefix="wt_", dir=_resolve_short_socket_base()) as raw_dir:
        yield Path(raw_dir)


def _write_wt_stub(bin_dir: Path, body: str) -> Path:
    """Write an executable `wt` shell stub into `bin_dir`."""
    stub = bin_dir / "wt"
    stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    stub.chmod(STUB_SCRIPT_MODE)
    return stub


def _compute_p95(samples: list[float]) -> float:
    """Compute the 95th-percentile latency in seconds from sorted sample timings."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(len(ordered) * PERCENTILE_95_FRACTION))
    return ordered[index]


# endregion


# region Shell Plugin Tests
def test_shell_files_exist_and_contain_zero_hardcoded_home_paths() -> None:
    """Verify all `shell/` prompt files exist and contain zero user home path literals."""
    expected_files = (
        STARSHIP_CONFIG_PATH,
        ZSH_PLUGIN_PATH,
        POWERSHELL_MODULE_PATH,
        SHELL_README_PATH,
    )
    for expected in expected_files:
        assert expected.is_file(), f"Missing shell integration file: {expected.name}"

    forbidden_prefixes = (
        "/" + "Users" + "/",
        "/" + "home" + "/",
        "~" + "/",
        "C:\\" + "Users" + "\\",
    )
    for shell_file in sorted(SHELL_DIR.rglob("*")):
        if not shell_file.is_file():
            continue
        content = shell_file.read_text(encoding="utf-8")
        for prefix in forbidden_prefixes:
            assert prefix not in content, f"{shell_file.name} contains forbidden prefix {prefix}"


def test_starship_toml_parses_and_renders_active_vs_offline_states(tmp_path: Path) -> None:
    """Verify `shell/starship.toml` configures `[custom.worktrack]` cleanly."""
    raw_snippet = STARSHIP_CONFIG_PATH.read_text(encoding="utf-8")
    parsed = tomllib.loads(raw_snippet)
    assert set(parsed.keys()) == {"custom"}
    merged = tomllib.loads(f'[character]\nsuccess_symbol = ">"\n\n{raw_snippet}')
    assert "worktrack" in merged["custom"]
    custom = parsed["custom"]["worktrack"]
    assert "wt status --porcelain" in str(custom["command"])
    assert "wt" in str(custom["when"])
    assert custom["ignore_timeout"] is True

    starship_bin = shutil.which("starship")
    if starship_bin is None:
        return

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write_wt_stub(bin_dir, "exit 0")
    base_env = {
        "STARSHIP_CONFIG": str(STARSHIP_CONFIG_PATH),
        "PATH": f"{bin_dir}:{Path(starship_bin).parent}:/usr/bin:/bin",
    }
    idle_res = subprocess.run(
        [starship_bin, "module", "custom.worktrack"],
        env=base_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert idle_res.stdout == ""

    _write_wt_stub(bin_dir, 'printf "PROJ-142 25m | today 2h30m\\n"')
    active_res = subprocess.run(
        [starship_bin, "module", "custom.worktrack"],
        env=base_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "PROJ-142 25m | today 2h30m" in active_res.stdout


def test_oh_my_zsh_plugin_syntax_and_execution_states(tmp_path: Path) -> None:
    """Verify `worktrack.plugin.zsh` passes `zsh -n` and handles active, idle, and offline."""
    zsh_bin = shutil.which("zsh")
    assert zsh_bin is not None, "Expected zsh binary for oh-my-zsh plugin verification"

    subprocess.run([zsh_bin, "-n", str(ZSH_PLUGIN_PATH)], check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cmd = f'source "{ZSH_PLUGIN_PATH}" && worktrack_prompt_info'

    no_wt_res = subprocess.run(
        [zsh_bin, "-c", cmd],
        env={"PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert no_wt_res.stdout == ""

    _write_wt_stub(bin_dir, "exit 1")
    offline_res = subprocess.run(
        [zsh_bin, "-c", cmd],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert offline_res.stdout == ""

    _write_wt_stub(bin_dir, 'printf "PROJ-142 25m | today 2h30m\\n"')
    active_res = subprocess.run(
        [zsh_bin, "-c", cmd],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "PROJ-142 25m | today 2h30m" in active_res.stdout


def test_powershell_module_contract_and_optional_pwsh_execution(tmp_path: Path) -> None:
    """Verify `WorktrackPrompt.psm1` exports `Get-WorktrackPromptSegment` safely."""
    content = POWERSHELL_MODULE_PATH.read_text(encoding="utf-8")
    assert "function Get-WorktrackPromptSegment" in content
    assert "Export-ModuleMember -Function Get-WorktrackPromptSegment" in content
    assert "Get-Command" in content
    assert "status --porcelain" in content
    assert content.count("{") == content.count("}")

    pwsh_bin = shutil.which("pwsh")
    if pwsh_bin is None:
        return

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write_wt_stub(bin_dir, 'printf "PROJ-142 25m | today 2h30m\\n"')
    pwsh_cmd = f'Import-Module "{POWERSHELL_MODULE_PATH}"; Get-WorktrackPromptSegment'
    res = subprocess.run(
        [pwsh_bin, "-NoProfile", "-Command", pwsh_cmd],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "PROJ-142 25m | today 2h30m" in res.stdout


# endregion


# region IPC Benchmarks
def test_bench_ipc_status_warm(
    benchmark: BenchmarkFixture,
    short_sock_dir: Path,
    tmp_path: Path,
) -> None:
    """Benchmark warm `worktrackd` Unix socket `status` query (`p95 <= 0.010s`)."""
    provider = StaticStatusProvider(
        initial_status=DaemonStatusSnapshot(
            active_activity_key="PROJ-142",
            active_activity_title="Warm Benchmark Activity",
            active_activity_kind="targeted",
            active_interval_elapsed_seconds=SAMPLE_WARM_ELAPSED,
            daily_total_seconds=SAMPLE_WARM_DAILY,
            workplace="home_office",
        )
    )
    recorder = TelemetryRecorder(telemetry_dir=tmp_path / "bench_telemetry")
    sock_path = short_sock_dir / "bench.sock"

    with WorktrackIpcServer(provider=provider, socket_path=sock_path, recorder=recorder):
        client = IpcClient(IpcEndpointConfig.for_unix(sock_path))
        _ = client.status()
        benchmark.group = "wt_ipc_status_warm"
        benchmark.extra_info["benchmark_id"] = "wt_ipc_status_warm"
        snapshot = benchmark(client.status)

    assert snapshot.active_activity_key == "PROJ-142"
    if benchmark.stats is not None:
        p95_seconds = _compute_p95(list(benchmark.stats.stats.data))
        benchmark.extra_info["p95_seconds"] = p95_seconds
        assert p95_seconds <= WARM_IPC_P95_BUDGET_SECONDS


# endregion
