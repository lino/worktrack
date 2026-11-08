# Architecture & Contributing

This guide is for contributors and developers who want to understand how Worktrack (`wt`) is structured internally, how its privacy guarantees are enforced in code, or how to build and test the project from source.

- Looking for everyday usage? See the **[Quickstart & Tracking Time](../user/guide.md)** guide.
- Looking for Prometheus metrics, health checks, or Docker setup? See **[Metrics Host, Health Checks & Docker](metrics-and-monitoring.md)**.

______________________________________________________________________

## Architecture Overview: How Privacy & Performance Work Under the Hood

Worktrack is built around three core mechanisms:

1. **Local-First Storage (`Ledger`)**: All raw time slices (`Intervals`), notes, work-location facts (`ContextFacts`), and local activity hints (`Clues`) are stored in a local SQLite database (`WAL` mode) on your workstation.
1. **Approval-Gated Outbox (`OutboxStore` & `ApprovedOutboxGrant`)**: External connectors (`JiraWorklogSink`, `CalendarFocusSink`, `GangliaHubSink`) cannot read your raw `Intervals` or send data on their own. First, `RollupEngine` aggregates and rounds your time into previewable `OutboxItem` proposals. Only when you approve an item does Worktrack issue a signed `ApprovedOutboxGrant` token allowing the connector to send that specific payload, recording a SHA-256 receipt in `EgressLedger`.
1. **Fast Shell-Prompt Helper (`worktrackd`)**: Spawning a Python interpreter on every shell prompt redraw would feel sluggish. When `wt daemon` is running, `wt status --porcelain` queries `worktrackd` over a local `0600` Unix domain socket (or loopback TCP on Windows) in under `10 ms`, and exits immediately if the daemon is stopped so your terminal never hangs.

```mermaid
flowchart LR
    subgraph Local["Workstation Node (SQLite WAL + OS Keychain)"]
        CLI["wt CLI / Shell Prompt / Vue 3 Cockpit"]
        Daemon["worktrackd IPC + SQLite Ledger"]
        Clues["ClueStore (Git / PRs / Calendar / Sensors)"]
        Reconcile["PlausibilityReconciler + RetroSplitEngine"]
        Outbox["RollupEngine -> OutboxStore -> EgressLedger"]
        Keyring["UserTokenStore (OS Keychain / 0600 Fallback)"]
    end

    subgraph External["External Services"]
        Broker["Stateless OAuth 2.0 PKCE Broker"]
        Jira["Jira Cloud REST API v3"]
        Cal["Calendars (iCal / JMAP / MS 365)"]
        Hub["Optional Team Hub"]
    end

    CLI <--> Daemon
    Clues --> Reconcile --> Daemon
    Daemon --> Outbox
    CLI <-->|"PKCE Code Exchange"| Broker
    Broker -->|"Tokens"| Keyring
    Outbox -->|"ApprovedOutboxGrant + SHA-256 Receipt"| Jira
    Outbox -->|"Opaque 'Focus Time' Busy"| Cal
    Outbox -->|"Aggregate Rollups Only"| Hub
```

______________________________________________________________________

## Codebase Domain Concepts

When reading or contributing to `src/worktrack/` and `frontend/src/`, you will encounter these domain models:

| Term                             | What It Represents in the Codebase                                                                   |
| -------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `Interval`                       | A recorded time slice (`[started_at, ended_at)`) in `structured` (clock in/out) or `chaotic` mode.   |
| `Activity`                       | A trackable unit of work: **Targeted** (e.g. Jira ticket `PROJ-142`) or **Standing** (e.g. Triage).  |
| `Entity` & `Attribution`         | Weighted many-to-many directed acyclic graph (`0.0 < weight <= 1.0`) linking tasks to projects/orgs. |
| `Facet` & `ContextFact`          | Typed local metadata (such as `workplace=home_office`) attached to a date or `Interval`.             |
| `Collector` / `Sensor` -> `Clue` | Local observers (Git commits, PRs/MRs, calendar events, network rules) that write local `Clues`.     |
| `Reconciler` & `RetroSplit`      | Plausibility detector and duration-conserving splitter (`proportional`, `equal`, `weights`).         |
| `Rollup` -> `Outbox` -> `Sink`   | Aggregator and staging queue that gates all outbound writes to external services (`Sinks`).          |
| `EgressLedger`                   | Append-only local audit log recording timestamp, destination URI, and SHA-256 payload digest.        |
| `Agenda` & `FocusBlock`          | Daily/weekly planner items and scheduled deep-work windows.                                          |
| `Node` / `Hub`                   | Personal workstation SQLite `Node` vs. optional shared Django/PostgreSQL `Hub` + OAuth Broker.       |

______________________________________________________________________

## Development Setup, Build & Test Commands

Worktrack uses:

- **Backend**: Python 3.13+, SQLite / PostgreSQL, Django 5, Django REST Framework, and `drf-spectacular` (OpenAPI 3.1).
- **Frontend**: TypeScript, Vue 3, Vuetify, `@hey-api/openapi-ts`, and `pinia-orm`.
- **Tooling**: [`mise`](https://mise.jdx.dev/), [`uv`](https://docs.astral.sh/uv/), and `pnpm`.

```bash
# Install toolchain and dependencies
mise install
mise run setup

# Run linters, strict type checks, and the full test suite
mise run verify
```

| Task Command            | What It Runs                                                                                |
| ----------------------- | ------------------------------------------------------------------------------------------- |
| `mise run setup`        | Initializes local cache directories and installs locked Python (`uv`) and frontend (`pnpm`) |
| `mise run openapi-sync` | Exports the OpenAPI 3.1 schema (`openapi.json`) and regenerates `@hey-api/openapi-ts` types |
| `mise run lint`         | Runs `ruff`, `mypy --strict`, frontend hygiene checks, and `vue-tsc --noEmit`               |
| `mise run test`         | Runs the Python `pytest` suite and frontend `vitest` suite                                  |
| `mise run verify`       | Runs both `mise run lint` and `mise run test`                                               |
| `mise run bench`        | Runs latency benchmarks for IPC status, Outbox rollup preview, attribution DAG, and secrets |
| `mise run docs`         | Builds the documentation site (`uvx zensical build --strict`) into `site/`                  |
