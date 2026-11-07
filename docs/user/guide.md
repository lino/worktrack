# Worktrack User Guide

This manual covers canonical Ubiquitous Language nomenclature, workstation quickstart, layered configuration without hardcoded paths, the `wt` CLI command suite, browser cockpit keyboard shortcuts, shell prompt plugins (`starship.rs`, `oh-my-zsh`, PowerShell), the anti-surveillance `Outbox` diff workflow, and local `workplace` `Facets`/`ContextFacts` for tax returns.

## Canonical Ubiquitous Language

| Domain Term                      | Definition & Privacy Boundary                                                                       |
| -------------------------------- | --------------------------------------------------------------------------------------------------- |
| `Interval`                       | Raw recorded time span supporting **structured** (`in`/`out`) and **chaotic** unallocated capture.  |
| `Activity`                       | Work classification unifying *Targeted Activities* (`PROJ-142`) and *Standing Activities* (triage). |
| `Entity` & `Attribution`         | Weighted DAG edge (`0.0 < weight <= 1.0`) attributing `Activities` across projects or customers.    |
| `Facet` & `ContextFact`          | Typed local metadata (`workplace=home_office`) asserted per date or `Interval` for tax reporting.   |
| `Collector` / `Sensor` -> `Clue` | Ambient local hints aiding retrospective reconciliation without automatic external reporting.       |
| `Reconciler` & `RetroSplit`      | Retroactive binding (`wt bind`) and contiguous splitting (`wt split`) of chaotic `Intervals`.       |
| `Rollup` -> `Outbox` -> `Sink`   | `collectd`-style bucketed aggregate staged in `Outbox` for user diff review prior to `Sink` egress. |
| `EgressLedger`                   | Immutable append-only audit log recording destination URI, payload SHA-256, and `FRITZ_TRACE_ID`.   |
| `Agenda` & `FocusBlock`          | Prioritized *Today* / *This Week* work queues, time budgets, deadlines, and scheduled focus blocks. |
| `Node` / `Hub`                   | Personal SQLite workstation `Node` vs. optional Django/PostgreSQL `Hub` and stateless OAuth Broker. |

## Quickstart & Toolchain Onboarding

Worktrack standardizes all developer, OpenAPI transport generation, and CI commands through `mise`, `uv`, and `pnpm`:

```bash
mise run setup
mise run openapi-sync
mise run verify
mise run docs
```

| Command                 | Purpose                                                                                     |
| ----------------------- | ------------------------------------------------------------------------------------------- |
| `mise run setup`        | Initialize `.cache/telemetry` and install locked Python (`uv`) and frontend (`pnpm`) deps.  |
| `mise run openapi-sync` | Export deterministic OpenAPI 3.1 schema and generate `@hey-api/openapi-ts` client types.    |
| `mise run fmt`          | Check Python formatting, Org/Markdown pipe tables, and Mermaid diagrams via `fritz fmt`.    |
| `mise run lint`         | Enforce `ruff`, `mypy --strict`, frontend TSDoc/complexity hygiene, `vue-tsc`, and `fritz`. |
| `mise run test`         | Execute automated behavioural `pytest` and frontend `vitest` test suites.                   |
| `mise run verify`       | Run combined lint, static typing, and behavioural test verification gates.                  |
| `mise run bench`        | Run `BENCH-SPEC-WT` latency benchmarks (`ledger-1k` attribution and warm `worktrackd` IPC). |
| `mise run docs`         | Build strict Zensical User and Operations manuals (`uvx zensical build --strict`).          |
| `mise run metrics`      | Emit OpenMetrics / Prometheus exposition from persisted telemetry state.                    |

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

## `wt` Command-Line Interface Reference

All `wt` subcommands support human-readable terminal output by default and `--json` for machine scripting:

| Subcommand  | Usage Example                                                              | Description                                                                   |
| ----------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `wt in`     | `wt in PROJ-142 --notes "Drafting Outbox spec"`                            | Start an active structured `Interval` (auto-creates local `Activity` if new). |
| `wt out`    | `wt out --notes "Completed slice"`                                         | Close the currently open `Interval` and compute its elapsed duration.         |
| `wt log`    | `wt log --start 2026-11-07T09:00:00Z --end 2026-11-07T10:30:00Z`           | Record a closed structured or unallocated **chaotic** `Interval`.             |
| `wt bind`   | `wt bind <interval_id> PROJ-142`                                           | Retroactively bind an unallocated chaotic `Interval` to an `Activity`.        |
| `wt split`  | `wt split <interval_id> --slice PROJ-142:1800 --slice PROJ-100:900`        | Split one `Interval` into contiguous attributed child `Intervals`.            |
| `wt status` | `wt status --porcelain`                                                    | Query active timer and daily total via fast `worktrackd` IPC or SQLite.       |
| `wt now`    | `wt now`                                                                   | Show active `Interval`, active/upcoming `FocusBlock`, and `workplace` fact.   |
| `wt today`  | `wt today`                                                                 | Show daily `Activity` totals, `Entity` attribution shares, and `Agenda`.      |
| `wt week`   | `wt week`                                                                  | Show weekly allocation breakdown, unallocated chaotic seconds, and deadlines. |
| `wt plan`   | `wt plan PROJ-142 --horizon today --priority 1 --budget-minutes 120`       | Schedule or update a prioritized `AgendaItem` in `today` or `this_week`.      |
| `wt focus`  | `wt focus --label "Deep Work" --start ... --end ... --expose-busy`         | Schedule a timeboxed `FocusBlock` with optional opaque calendar busy flag.    |
| `wt fact`   | `wt fact set workplace home_office --date 2026-11-07`                      | Assert a date- or interval-scoped `ContextFact` (or register custom `Facet`). |
| `wt entity` | `wt entity add customer BABELSBERG --name "Studio Babelsberg"`             | Register a non-strict `Entity` node (`project`, `customer`, `component`).     |
| `wt attr`   | `wt attr add --activity PROJ-142 --target customer:babelsberg --weight 1`  | Connect an `Activity` or `Entity` to a target `Entity` with a DAG weight.     |
| `wt outbox` | `wt outbox preview --stage` / `wt outbox approve <id>` / `wt outbox flush` | Preview `Rollup` diffs, approve/reject staged items, or flush to `Sinks`.     |
| `wt jira`   | `wt jira sync-issues --jql "assignee = currentUser()"`                     | Import Jira Cloud v3 issues into assignable `Activities` and `Entities`.      |
| `wt serve`  | `wt serve --host 127.0.0.1 --port 8000`                                    | Run the local Django + DRF + `drf-spectacular` API and browser cockpit host.  |
| `wt daemon` | `wt daemon --once`                                                         | Run the low-latency `worktrackd` Unix/TCP IPC socket server.                  |

## Browser Cockpit & Keyboard Shortcuts

The Vue 3 + Vuetify + `@hey-api/openapi-ts` + `pinia-orm` cockpit (`frontend/`) is designed for keyboard-first navigation with progressive disclosure:

| Shortcut / Chord | Action                                                                                           |
| ---------------- | ------------------------------------------------------------------------------------------------ |
| `Ctrl+K` / `:`   | Open the native `<dialog>` **Command Palette** modal to jump across views or trigger actions.    |
| `?`              | Open the native `<dialog>` **Keyboard Shortcut Help** overlay.                                   |
| `f`              | Switch to **Now & Insights** (active `Interval`, `Entity` attribution shares, `workplace` fact). |
| `p`              | Switch to **Today / Week Agenda & FocusBlocks** (prioritized queues, budgets, deadlines).        |
| `c`              | Switch to **Chaotic Interval & Attribution Editor** (record/bind intervals and DAG weights).     |
| `o`              | Switch to **Outbox Diff Preview & EgressLedger** (approve/reject rollups, inspect SHA-256 logs). |
| `Esc` / Backdrop | Dismiss any open modal `<dialog closedby="any">` and return focus to the invoking control.       |

Single-key chords (`c`, `p`, `o`, `f`, `?`, `:`) are automatically suppressed while typing inside `<input>`, `<textarea>`, `<select>`, or `contenteditable` fields and during active IME composition (`event.isComposing`).

## Cross-Shell Prompt Integrations (`shell/`)

Worktrack ships non-blocking shell prompt plugins in `shell/` that invoke `wt status --porcelain` (with a 50ms socket timeout so your terminal prompt never hangs when `worktrackd` is stopped):

1. **`starship.rs` (`shell/starship.toml`)**:
   Append the `[custom.worktrack]` block from `shell/starship.toml` to `$XDG_CONFIG_HOME/starship.toml`.
1. **`oh-my-zsh` (`shell/worktrack.plugin.zsh`)**:
   Source `shell/worktrack.plugin.zsh` in `$ZDOTDIR/.zshrc` and add `$(worktrack_prompt_info)` to `PROMPT` or `RPROMPT`.
1. **PowerShell (`shell/worktrack.ps1`)**:
   Dot-source `shell/worktrack.ps1` in `$PROFILE` and call `Get-WorktrackPromptSegment` inside your `prompt` function.

## Privacy Boundary & `Outbox` Diff Preview Guide

Worktrack enforces an architectural anti-surveillance gate between your private SQLite ledger and external `Sinks` (such as Jira Cloud worklogs):

1. **Local Redaction & Rounding (`RollupPolicy`)**: Raw `Intervals`, fuzzy chaotic notes, and `ContextFacts` remain strictly on your workstation. When you run `wt outbox preview --stage` (or open the **Outbox Diff** view via `o`), `RollupEngine` buckets `Intervals` by window (`day`, `week`, `month`, `interval`), rounds durations (`nearest`, `ceil`, `floor` to configurable quantum seconds), strips raw notes when `redact_notes = true`, and maps *Standing Activities* to their configured `rollup_target_ref`.
1. **Explicit Approval Gate (`ApprovedOutboxGrant`)**: Every staged `OutboxItem` displays its before/after duration delta and exact outbound JSON preview. No `Sink` adapter can transmit data without a cryptographically sealed `ApprovedOutboxGrant` token minted by approving the `OutboxItem`.
1. **Immutable Audit Trail (`EgressLedger`)**: Every flush attempt records an append-only `EgressRecord` containing the UTC timestamp, destination URI, canonical payload SHA-256 digest, HTTP status, external receipt ID, and W3C `FRITZ_TRACE_ID`.

## Custom `workplace` `Facets` & `ContextFacts` for Tax Returns

Worktrack seeds the built-in `workplace` `Facet` (`enum[home_office, office, mobile_work]`) automatically so you can record daily or per-interval location facts for annual tax deductions without leaking location data to external ticket trackers:

```bash
# Assert today's workplace for tax reporting
wt fact set workplace home_office --date 2026-11-07

# Register a custom Facet schema and assert a ContextFact
wt fact register-facet billable_rate_tier --type enum --values standard, overtime, research
wt fact set billable_rate_tier research --date 2026-11-07
```

## CLI Telemetry & `Schaltwerk` Workflow

Render persisted Prometheus metrics directly to standard output or serve `/metrics` and `/healthz` over loopback HTTP:

```bash
uv run python -m worktrack.telemetry
uv run python -m worktrack.telemetry --serve --host 127.0.0.1 --port 9112
```

Contributors working inside F.R.I.T.Z. execute approved Work Packages (`L1..L5`) across `gemini`, `claude`, `gpt`, `gpt-oss`, and `local` model families (`aws-bedrock` is disabled at project root):

1. Inspect approved waves via `fritz ready --waves`.
1. Execute bounded TDD slices inside isolated `git worktree` checkouts (`Red -> Green -> Refactor`).
1. Keep functions `<= 50` statement lines, McCabe cyclomatic complexity `<= 10`, and 100% Google docstrings / TSDoc.
