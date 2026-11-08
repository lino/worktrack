# Worktrack User Guide

This guide walks you through using Worktrack (`wt`) in your daily routine — from starting your first timer and planning your day to syncing with Jira, blocking focus time on your calendar, and exporting tax or project reports.

If you are looking for build instructions, internal architecture details, or contributor workflows, see the **[Developer & Architecture Guide](../developer/guide.md)**.

______________________________________________________________________

## Quickstart: Your First 5 Minutes

### 1. Track a task while you work

When you sit down to work on something, tell Worktrack what you are starting. When you finish (or switch tasks), clock out:

```bash
# Start timing a task (optionally link a ticket key like PROJ-142)
wt in "Fix login redirect" --ref PROJ-142

# Check what you are currently working on
wt now

# Stop the timer and add a quick note for yourself
wt out --note "Added regression test"
```

### 2. Forgot to start a timer? Log it after the fact

Real workdays are messy. If you spent the afternoon debugging and forgot to run a timer, you can log that block of time later and split it across the tasks you worked on:

```bash
# Log an unallocated afternoon block
wt log "Afternoon debugging and review" --at 2026-11-08T13:00:00Z --end 2026-11-08T15:00:00Z --chaotic

# Split that block across two tasks later
wt split <interval-id> --slice PROJ-142:4500 --slice PROJ-100:2700
```

Or if the whole block belonged to a single task, assign it in one step:

```bash
wt bind <interval-id> PROJ-142
```

### 3. See where your time went today and this week

Check your progress at any point during the day or at the end of the week:

```bash
wt today
wt week
```

______________________________________________________________________

## Planning Your Day and Protecting Focus Time

### Why use it?

Between meetings, messages, and ticket queues, it is easy to lose track of what you actually intended to finish today. Worktrack gives you a lightweight planner for **Today** and **This Week**, plus **Focus Blocks** to reserve uninterrupted time for deep work.

### How to plan your priorities (`wt plan`)

Add tasks to your **Today** or **This Week** list with an optional time budget (in minutes):

```bash
# Plan 2 hours on PROJ-142 today as your #1 priority
wt plan PROJ-142 --horizon today --priority 1 --budget-minutes 120

# Put a lower-priority task on your radar for this week
wt plan PROJ-205 --horizon this_week --priority 2 --budget-minutes 90
```

### How to block out focus time (`wt focus`)

Reserve a window for uninterrupted work. If you pass `--expose-busy`, Worktrack can also publish that window to your work calendar as a simple `"Focus Time"` busy block so colleagues don't book meetings over it:

```bash
wt focus \
  --label "Deep Work: API Migration" \
  --start 2026-11-08T09:00:00Z \
  --end 2026-11-08T11:00:00Z \
  --expose-busy
```

______________________________________________________________________

## Filling In the Blanks with Local Hints (`wt clue` & `wt reconcile`)

### Why use it?

At the end of a busy day, trying to remember which tickets you worked on between 2:00 PM and 5:00 PM is tedious. Because you already leave traces in your local Git commits and pull requests, Worktrack can read those hints locally and suggest how to divide your unallocated time.

### Step 1: Scan your Git commits for ticket hints (`wt clue`)

Scan a local Git repository for commit messages that mention ticket keys (such as `PROJ-142`). These hints stay entirely on your computer and never automatically book time for you:

```bash
# Scan a local Git repository for recent commits
wt clue git-sync --repo "$HOME/work/my-repo"

# View the hints collected on your computer
wt clue list --source git
```

### Step 2: Reconcile unbooked time (`wt reconcile`)

Ask Worktrack to check for tasks you touched (via Git commits or completed agenda items) that don't have time logged yet, and split an unallocated time block across them:

```bash
# 1. See which tasks have activity hints but no logged time
wt reconcile inspect

# 2. Preview how an unallocated block would be split across two tasks
wt reconcile propose \
  --interval-id <interval-id> \
  --activity PROJ-142 \
  --activity PROJ-100 \
  --strategy proportional

# 3. Apply the split when it looks right
wt reconcile apply \
  --interval-id <interval-id> \
  --activity PROJ-142 \
  --activity PROJ-100 \
  --strategy proportional
```

______________________________________________________________________

## Connecting to Jira Cloud (Login, Sync & Worklogs)

### Why use it?

If your team tracks work in Jira, copying hours from your notes into Jira at the end of the week is a chore. Worktrack lets you:

1. **Pull your assigned Jira issues** so you can track time against them right away.
1. **Preview and send rounded worklogs back to Jira** when you are ready — without ever exposing your raw minute-by-minute timeline or personal scratch notes.

Your Jira login credentials are saved safely in your computer's native password manager (**macOS Keychain**, **Linux Secret Service / KWallet**, or **Windows Credential Manager**).

### Step 1: Set your Jira Cloud site ID

Tell Worktrack which Jira Cloud site you use by setting `WORKTRACK_JIRA_CLOUD_ID` in your shell profile:

```bash
export WORKTRACK_JIRA_CLOUD_ID="your-atlassian-cloud-id"
```

> **Tip — How to find your Jira Cloud ID:**
> While signed in to Jira in your browser, open `https://<your-company>.atlassian.net/_edge/tenant_info`. You will see `{"cloudId": "01234567-89ab-cdef-0123-456789abcdef"}`.

### Step 2: Log in to Jira (`wt jira login`)

You can log in either through your browser (OAuth 2.0) or by saving an access token directly.

#### Option A: Browser Login (OAuth 2.0)

1. Generate your login link:

   ```bash
   export WORKTRACK_JIRA_CLIENT_ID="your-oauth-client-id"
   export WORKTRACK_JIRA_REDIRECT_URI="http://127.0.0.1:5173/callback"

   wt jira auth-url --json
   ```

1. Open the `authorization_url` in your browser and click **Accept**.

1. Copy the `code` from the redirect address bar and finish logging in:

   ```bash
   wt jira login --code "<code-from-redirect>" --code-verifier "<code_verifier>"
   ```

#### Option B: Direct Token Login

If you already have a Jira OAuth access token (and optional refresh token), save it directly to your computer's keychain:

```bash
wt jira login \
  --access-token "<jira-oauth-access-token>" \
  --refresh-token "<optional-refresh-token>"
```

#### Check your login status or sign out

```bash
# Check whether you are logged in
wt jira status

# Sign out and remove your stored token from the keychain
wt jira logout
```

### Step 3: Pull your assigned Jira issues (`wt jira sync`)

Import your open Jira issues into Worktrack so you can clock in against them by issue key:

```bash
# Pull open issues assigned to you
wt jira sync

# Or use a custom JQL filter
wt jira sync --jql 'assignee = currentUser() AND statusCategory != Done'
```

### Step 4: Review and send worklogs via the Outbox (`wt outbox`)

Worktrack never sends time to Jira behind your back. Instead, you stage your hours in the **Outbox**, review the rounded totals, approve the items you want to share, and send them:

```bash
# 1. Group and round your tracked time into the Outbox for review
wt outbox preview --sink jira --stage --cadence eod

# 2. See what is waiting in your Outbox and approve it
wt outbox list
wt outbox approve <outbox-item-id>

# 3. Send approved worklogs to Jira
wt outbox flush --sink jira

# 4. View a log of everything you have sent so far
wt outbox ledger
```

______________________________________________________________________

## Syncing Your Calendar (`wt calendar`)

### Why use it?

Meetings take up a real chunk of your week, and deep work requires defending your calendar from new meeting invites. Worktrack can import your calendar events so your meeting time is accounted for, and can publish your scheduled **Focus Blocks** back to your calendar as `"Focus Time"`.

```bash
# Import meetings from a calendar file (.ics), Fastmail (JMAP), or Microsoft 365
wt calendar sync --ics-file "$XDG_DATA_HOME/worktrack/cal.ics"

# Stage your scheduled Focus Blocks to be published as "Focus Time" on your calendar
wt calendar stage-focus --start 2026-11-08T00:00:00Z --end 2026-11-09T00:00:00Z
```

Just like Jira worklogs, staged calendar blocks wait in your **Outbox** (`wt outbox list`) until you approve and send them.

______________________________________________________________________

## Work-Location Tracking & Reports (`wt fact`, `wt sensor`, `wt export`)

### Why use it?

1. **Tax returns**: Many countries let you deduct home-office days on your annual tax return, provided you have a daily log of when you worked from home vs. the office.
1. **Multi-project & customer reporting**: Sometimes a single task benefits two customers or two internal projects (for example, 60% Project A and 40% Project B). Worktrack lets you link tasks across projects, customers, or cost centers and generate clean breakdowns.

### Recording where you worked (`wt fact` & `wt sensor`)

You can log your work location (`home_office`, `office`, or `mobile_work`) manually with one command, or let Worktrack detect it from local rules (such as your home Wi-Fi name, office network, VPN, or connected monitors):

```bash
# Set today's work location manually
wt fact set workplace home_office --date 2026-11-08

# Or let Worktrack check your local Wi-Fi / network rules and set it for you
wt sensor evaluate --date 2026-11-08 --apply
```

You can also define your own custom tags (for example, `billable_rate_tier`) if you want to track extra details:

```bash
wt fact register-facet billable_rate_tier --type enum --values standard, overtime, research
wt fact set billable_rate_tier research --date 2026-11-08
```

### Linking tasks to projects or customers (`wt entity` & `wt attr`)

Group your tasks under customers, projects, epics, or cost centers — even when a task is shared across more than one:

```bash
# Create a customer or project entry
wt entity add customer ACME --name "Acme Corp"

# Link task PROJ-142 to that customer
wt attr add --activity PROJ-142 --target customer:acme --weight 1
```

### Exporting tax and project reports (`wt export`)

Generate ready-to-use summaries in `Markdown`, `CSV` (for spreadsheets), or `JSON`:

```bash
# Export your annual home-office / office / mobile-work summary for tax season
wt export tax --year 2026 --format markdown

# Export a project and customer time breakdown for a date range
wt export attribution --start 2026-11-01T00:00:00Z --end 2026-11-08T00:00:00Z --format csv
```

______________________________________________________________________

## Using the Web App (Browser Cockpit)

### Why use it?

While the `wt` command line is great for quick actions, the **Browser Cockpit** gives you a visual overview of your day, week, unallocated time blocks, and Outbox previews — all navigable with fast keyboard shortcuts.

Start the local web server:

```bash
wt serve --host 127.0.0.1 --port 8000
```

### Keyboard Shortcuts in the Web App

| Shortcut       | What It Does                                                           |
| -------------- | ---------------------------------------------------------------------- |
| `Ctrl+K` / `:` | Open the **Command Palette** to search and run any action              |
| `?`            | Show the **Keyboard Shortcut Help** window                             |
| `f`            | Go to **Now & Insights** (active timer, daily progress, work-location) |
| `p`            | Go to **Today & This Week Planner** (priorities and Focus Blocks)      |
| `c`            | Go to **Time Block Splitter & Project Links**                          |
| `o`            | Go to **Outbox Review & Sent History**                                 |
| `s`            | Go to **End-of-Day Reconciler, Hints & Report Exports**                |
| `Esc`          | Close any open dialog window                                           |

______________________________________________________________________

## Showing Your Active Timer in Your Terminal Prompt

### Why use it?

When you can see `[PROJ-142 0:42]` right in your shell prompt, you always know whether a timer is running and never forget to clock out when switching tasks.

1. Start the background helper once in your session:

   ```bash
   wt daemon --background
   ```

1. Add the prompt snippet for your shell (found in the `shell/` folder):

   - **`starship.rs`**: Copy the `[custom.worktrack]` block from `shell/starship.toml` into `$XDG_CONFIG_HOME/starship.toml`.
   - **`oh-my-zsh`**: Source `shell/worktrack.plugin.zsh` in `$ZDOTDIR/.zshrc` and add `$(worktrack_prompt_info)` to `PROMPT` or `RPROMPT`.
   - **PowerShell**: Dot-source `shell/worktrack.ps1` in `$PROFILE` and call `Get-WorktrackPromptSegment` inside your `prompt` function.

______________________________________________________________________

## Optional: AI Assistants (`wt mcp`) & Team Project Lists (`wt hub`)

### Using Worktrack with AI Assistants (`wt mcp`)

If you use a local AI coding assistant that supports the **Model Context Protocol (MCP)**, you can connect it to Worktrack so you can ask questions like *"What did I work on today?"* or *"Draft a time split for my afternoon block"*.

```bash
wt mcp
```

For your safety, the AI assistant can only read your local schedule and place draft proposals into your **Outbox** — it can never send anything to Jira or your calendar without your manual approval.

### Sharing Project Lists with a Team (`wt hub`)

If your team runs a shared Worktrack Hub server, you can pull your team's shared list of project and customer names so everyone doesn't have to set them up by hand:

```bash
# Check connection and shared rollup status
wt hub status

# Pull shared project and customer names into your local setup
wt hub pull-catalog --hub-url https://hub.example.invalid
```

______________________________________________________________________

## Command Cheat Sheet

Every `wt` command prints human-friendly text by default, or structured JSON if you add `--json`.

| Goal                             | Command        | Example                                                          |
| -------------------------------- | -------------- | ---------------------------------------------------------------- |
| Start a timer                    | `wt in`        | `wt in "Draft spec" --ref PROJ-142`                              |
| Stop the active timer            | `wt out`       | `wt out --note "Finished first draft"`                           |
| Log a past block of time         | `wt log`       | `wt log "Debugging" --at 2026-11-08T09:00:00Z --end ...`         |
| Assign an unallocated block      | `wt bind`      | `wt bind <interval_id> PROJ-142`                                 |
| Split a block across tasks       | `wt split`     | `wt split <interval_id> --slice PROJ-142:1800 --slice ...`       |
| Check active timer & daily total | `wt status`    | `wt status`                                                      |
| See what is happening right now  | `wt now`       | `wt now`                                                         |
| See today's summary & plan       | `wt today`     | `wt today`                                                       |
| See this week's summary & plan   | `wt week`      | `wt week`                                                        |
| Add a task to Today / This Week  | `wt plan`      | `wt plan PROJ-142 --horizon today --priority 1`                  |
| Block out deep-work focus time   | `wt focus`     | `wt focus --label "Deep Work" --start ... --end ...`             |
| Log where you worked today       | `wt fact`      | `wt fact set workplace home_office --date 2026-11-08`            |
| Auto-detect where you worked     | `wt sensor`    | `wt sensor evaluate --date 2026-11-08 --apply`                   |
| Scan Git commits for task hints  | `wt clue`      | `wt clue git-sync --repo "$HOME/work/repo"`                      |
| Reconcile unbooked time          | `wt reconcile` | `wt reconcile inspect`                                           |
| Log in & sync with Jira Cloud    | `wt jira`      | `wt jira login ...` / `wt jira sync`                             |
| Review & send hours to Jira      | `wt outbox`    | `wt outbox preview --stage` / `wt outbox approve <id>`           |
| Import calendar or publish focus | `wt calendar`  | `wt calendar sync --ics-file "$XDG_DATA_HOME/worktrack/cal.ics"` |
| Add a project or customer        | `wt entity`    | `wt entity add customer ACME --name "Acme Corp"`                 |
| Link a task to a project         | `wt attr`      | `wt attr add --activity PROJ-142 --target customer:acme`         |
| Export tax or project reports    | `wt export`    | `wt export tax --year 2026 --format markdown`                    |
| Start the local web app server   | `wt serve`     | `wt serve --host 127.0.0.1 --port 8000`                          |
| Start the shell-prompt helper    | `wt daemon`    | `wt daemon --background`                                         |
| Connect a local AI assistant     | `wt mcp`       | `wt mcp`                                                         |
| Sync shared team project catalog | `wt hub`       | `wt hub pull-catalog --hub-url https://hub.example.invalid`      |
