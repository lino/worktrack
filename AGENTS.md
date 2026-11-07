# Worktrack (`wt`) — F.R.I.T.Z. Workspace Agent Rules

`worktrack` is governed in **Studio Babelsberg Hub mode** (`$BABELSBERG_HUB/projects/worktrack/` and `$BABELSBERG_HUB/planning/work/worktrack/work-packages.org`).

## Core Domain & Craft Invariants

1. **Canonical Nomenclature**: Always use `Interval`, `Activity` (*Targeted* vs. *Standing*), `Entity` & `Attribution`, `Facet` & `ContextFact`, `Collector` / `Sensor` -> `Clue`, `Reconciler` & `RetroSplit`, `Rollup` -> `Outbox` -> `Sink` -> `EgressLedger`, `Agenda` & `FocusBlock`, and `Node` / `Hub`.
1. **Anti-Surveillance & Outbox Gate**: Never transmit raw `Intervals`, notes, or local `Clues` directly to external `Sinks`; all outbound payloads must stage through `Outbox` with previewable diffs and record SHA-256 receipts in `EgressLedger`.
1. **Secret Isolation (`PROB-WT-001`)**: Resolve application OAuth secrets via `SecretProvider` URIs (`env://`, `file://`, `keyring://`, optional `aws-sm://` in `worktrack[aws]`); store per-user OAuth tokens strictly in the local OS Keyring (`UserTokenStore`), never on the server. Keep `cloud/aws` off at project root (never use `aws-bedrock`).
1. **Book-Like Readability & TDD**: Keep functions `<= 50` statement lines, McCabe complexity `<= 10`, files `~450` lines, 100% Google docstrings (`pydoc` / TSDoc), JetBrains `# region` comments tracked in uncommitted `.regions.md` when `> 3` sections exist, zero magic values, zero hardcoded paths, and strict Red-to-Green TDD via `mise run test` & `mise run verify`.
1. **Standard Web Stack**: Backend uses Python 3.13+ + Django + DRF + `drf-spectacular`; frontend uses TypeScript + Vue 3 + Vuetify + `@hey-api/openapi-ts` + `pinia-orm`.
1. **Zero Self-Approval (`Freigabeamt`)**: Only execute Work Packages approved by a human counter-signature (`./bin/kb approval swipe` in `$BABELSBERG_HUB`), dispatching conflict-free waves (`fritz ready --waves`) in parallel isolated worktrees (`Workspace="share"`).
