# Worktrack User Guide

This manual covers workstation setup, layered configuration without hardcoded paths, F.R.I.T.Z. `Schaltwerk` development workflows, and local telemetry inspection.

## Quickstart & Toolchain Onboarding

Worktrack standardizes all developer and CI commands through `mise` and `uv`:

```bash
mise run setup
mise run verify
mise run docs
```

| Command            | Purpose                                                                    |
| ------------------ | -------------------------------------------------------------------------- |
| `mise run setup`   | Create `.cache/telemetry` and install locked Python dependencies via `uv`. |
| `mise run fmt`     | Check Org/Markdown pipe tables and Mermaid diagrams via `fritz fmt`.       |
| `mise run lint`    | Enforce Google style (`ruff`), `mypy --strict`, and `fritz validate`.      |
| `mise run test`    | Run automated behavioural `pytest` suites.                                 |
| `mise run verify`  | Execute combined lint, static typing, and behavioural test gates.          |
| `mise run docs`    | Build strict Zensical User and Operations manuals (`--strict`).            |
| `mise run metrics` | Emit OpenMetrics / Prometheus exposition from persisted state.             |

## Layered Configuration Reference

`WorktrackSettings` resolves settings across three deterministic layers (**Environment Variables** > **TOML Config File** > **XDG Base Directories**) with zero hardcoded user paths:

| Setting                | Environment Variable                 | TOML Key (`[worktrack]`)   | Default XDG Resolution                       |
| ---------------------- | ------------------------------------ | -------------------------- | -------------------------------------------- |
| Configuration File     | `WORKTRACK_CONFIG_FILE`              | *(file path)*              | `$XDG_CONFIG_HOME/worktrack/config.toml`     |
| Local Ledger Database  | `WORKTRACK_DB_PATH`                  | `db_path`                  | `$XDG_DATA_HOME/worktrack/ledger.sqlite3`    |
| Daemon IPC Socket      | `WORKTRACK_SOCKET_PATH`              | `socket_path`              | `$XDG_RUNTIME_DIR/worktrack/worktrackd.sock` |
| Telemetry State Dir    | `WORKTRACK_TELEMETRY_DIR`            | `telemetry_dir`            | `$XDG_STATE_HOME/worktrack/telemetry`        |
| Metrics HTTP Bind Host | `WORKTRACK_METRICS_HOST`             | `metrics_host`             | `127.0.0.1`                                  |
| Metrics HTTP TCP Port  | `WORKTRACK_METRICS_PORT`             | `metrics_port`             | `9112`                                       |
| Metrics Shutdown Wait  | `WORKTRACK_METRICS_SHUTDOWN_TIMEOUT` | `metrics_shutdown_timeout` | `5.0`                                        |
| Deployment Topology    | `WORKTRACK_MODE`                     | `mode`                     | `node` (`node` or `hub`)                     |
| Log Level              | `WORKTRACK_LOG_LEVEL`                | `log_level`                | `INFO`                                       |
| Label Value Ceiling    | `WORKTRACK_MAX_LABEL_VALUES`         | `max_label_values`         | `20`                                         |
| Series Ceiling         | `WORKTRACK_MAX_SERIES_PER_METRIC`    | `max_series_per_metric`    | `500`                                        |

Example `config.toml`:

```toml
[worktrack]
mode = "node"
metrics_host = "127.0.0.1"
metrics_port = 9112
log_level = "INFO"
```

## CLI Telemetry & Health Inspection

Render persisted Prometheus metrics directly to standard output:

```bash
uv run python -m worktrack.telemetry
```

Or serve `/metrics` and `/healthz` over loopback HTTP:

```bash
uv run python -m worktrack.telemetry --serve --host 127.0.0.1 --port 9112
```

## `Schaltwerk` Routing & Skills Workflow

Contributors working inside F.R.I.T.Z. execute approved Work Packages (`L1..L5`) across `gemini`, `claude`, `gpt`, `gpt-oss`, and `local` model families (`aws-bedrock` is disabled at project root):

1. Inspect approved waves via `fritz ready --waves`.
1. Execute bounded TDD slices inside isolated `git worktree` checkouts (`Red -> Green -> Refactor`).
1. Keep functions `<= 50` statement lines, McCabe cyclomatic complexity `<= 10`, and 100% Google docstrings.
