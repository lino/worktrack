# Quickstart & Tracking Time

This page covers the core of Worktrack (`wt`): starting and stopping timers, logging blocks of time after the fact, and checking your daily or weekly progress. Every other feature (Jira, calendars, tax exports, shell prompts) is optional and covered in its own guide.

______________________________________________________________________

## 1. Track a Task While You Work (`wt in` & `wt out`)

When you start working on something, clock in. When you finish or switch tasks, clock out:

```bash
# Start timing a task (optionally attach a ticket reference like PROJ-142)
wt in "Fix login redirect" --ref PROJ-142

# See what you are currently working on
wt now

# Stop the timer and save a note for yourself
wt out --note "Added regression test"
```

______________________________________________________________________

## 2. Log Time After the Fact (`wt log`, `wt bind` & `wt split`)

### Why use it?

You don't have to remember to start a timer every time your day gets busy. If you spent the afternoon debugging and answering questions without a timer running, you can record that block of time afterward and assign or split it whenever you are ready.

```bash
# Record an unallocated block from 1:00 PM to 3:00 PM
wt log "Afternoon debugging and review" \
  --at 2026-11-08T13:00:00Z \
  --end 2026-11-08T15:00:00Z \
  --chaotic

# Assign the whole block to a single task...
wt bind <interval-id> PROJ-142

# ...or split it across two different tasks (durations in seconds or minutes)
wt split <interval-id> --slice PROJ-142:4500 --slice PROJ-100:2700
```

______________________________________________________________________

## 3. Check Your Progress (`wt status`, `wt today` & `wt week`)

See where your hours went today or across the current week:

```bash
# Quick one-line status of your active timer and today's total
wt status

# Detailed breakdown for today or this week
wt today
wt week
```

______________________________________________________________________

## Where Your Data Is Stored (Optional Configuration)

Out of the box, Worktrack stores your timeline in your standard user data folder with zero setup required. You only need these settings if you want to keep your database or config file in a custom folder:

| What It Controls        | Environment Variable    | Config Key (`[worktrack]`) | Default Location                          |
| ----------------------- | ----------------------- | -------------------------- | ----------------------------------------- |
| Configuration file path | `WORKTRACK_CONFIG_FILE` | *(file path)*              | `$XDG_CONFIG_HOME/worktrack/config.toml`  |
| Local time database     | `WORKTRACK_DB_PATH`     | `db_path`                  | `$XDG_DATA_HOME/worktrack/ledger.sqlite3` |

______________________________________________________________________

## All `wt` Commands at a Glance

Add `--json` to any command if you want machine-readable JSON output for scripts.

| Goal                             | Command                          | Guide                                                                    |
| -------------------------------- | -------------------------------- | ------------------------------------------------------------------------ |
| Start / stop a live timer        | `wt in` / `wt out`               | This page                                                                |
| Log or split past time blocks    | `wt log` / `wt split`            | This page                                                                |
| Assign an unallocated block      | `wt bind`                        | This page                                                                |
| Check now / today / this week    | `wt now` / `wt today`            | This page                                                                |
| Plan today / week & focus blocks | `wt plan` / `wt focus`           | [Planning, Focus Time & Reconciling](planning-and-reconciliation.md)     |
| Scan Git commits for task hints  | `wt clue`                        | [Planning, Focus Time & Reconciling](planning-and-reconciliation.md)     |
| Reconcile unbooked time blocks   | `wt reconcile`                   | [Planning, Focus Time & Reconciling](planning-and-reconciliation.md)     |
| Log in & sync with Jira Cloud    | `wt jira`                        | [Jira Cloud Login, Sync & Outbox](jira-and-outbox.md)                    |
| Preview & send approved hours    | `wt outbox`                      | [Jira Cloud Login, Sync & Outbox](jira-and-outbox.md)                    |
| Import calendar or publish focus | `wt calendar`                    | [Calendars, Work Location & Reports](calendars-and-reports.md)           |
| Log or auto-detect work location | `wt fact` / `wt sensor evaluate` | [Calendars, Work Location & Reports](calendars-and-reports.md)           |
| Group tasks by project/customer  | `wt entity` / `wt attr`          | [Calendars, Work Location & Reports](calendars-and-reports.md)           |
| Export tax or project reports    | `wt export`                      | [Calendars, Work Location & Reports](calendars-and-reports.md)           |
| Launch the web app               | `wt serve`                       | [Web Cockpit, Shell Prompts, AI & Team Hub](cockpit-and-integrations.md) |
| Show active timer in prompt      | `wt daemon` / `wt status`        | [Web Cockpit, Shell Prompts, AI & Team Hub](cockpit-and-integrations.md) |
| Connect a local AI assistant     | `wt mcp`                         | [Web Cockpit, Shell Prompts, AI & Team Hub](cockpit-and-integrations.md) |
| Sync shared team project catalog | `wt hub`                         | [Web Cockpit, Shell Prompts, AI & Team Hub](cockpit-and-integrations.md) |
