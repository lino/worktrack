# Planning, Focus Time & Reconciling Your Day

This guide explains how to plan your priorities for **Today** and **This Week**, protect uninterrupted blocks for deep work, and use local hints from your Git commits to split unbooked time at the end of the day.

______________________________________________________________________

## 1. Planning Your Day & Week (`wt plan`)

### Why use it?

Between chat messages, meetings, and incoming tickets, it is easy to finish the day without touching the task you actually intended to complete. Worktrack gives you a lightweight priority list for **Today** and **This Week** with optional time budgets so you can compare what you planned against where your time actually went.

### How to use it

```bash
# Plan 2 hours (120 minutes) on PROJ-142 today as your #1 priority
wt plan PROJ-142 --horizon today --priority 1 --budget-minutes 120

# Put a second task on your plan for this week
wt plan PROJ-205 --horizon this_week --priority 2 --budget-minutes 90

# View today's and this week's agenda alongside your tracked hours
wt today
wt week
```

______________________________________________________________________

## 2. Blocking Out Focus Time (`wt focus`)

### Why use it?

Deep work requires uninterrupted time. Scheduling a **Focus Block** lets you set aside a specific window on your schedule — and if you pass `--expose-busy`, Worktrack can stage that block to be published on your work calendar as a simple `"Focus Time"` busy slot so colleagues don't book meetings over it.

### How to use it

```bash
wt focus \
  --label "Deep Work: API Migration" \
  --start 2026-11-08T09:00:00Z \
  --end 2026-11-08T11:00:00Z \
  --expose-busy
```

______________________________________________________________________

## 3. Reconstructing Busy Afternoons (`wt clue` & `wt reconcile`)

### Why use it?

When you are deep in code all afternoon, starting and stopping timers for every commit or code review breaks your flow. Instead, you can log a single unallocated afternoon block (`wt log ... --chaotic`) and let Worktrack look at the ticket keys in your local Git commits to suggest how to divide that time.

### Step 1: Scan local Git commits for ticket hints (`wt clue`)

Worktrack scans your local Git repository for commit messages mentioning ticket keys (like `PROJ-142`) and saves them as local hints. Nothing leaves your computer, and no time is booked automatically:

```bash
# Scan a local Git repository for commit hints
wt clue git-sync --repo "$HOME/work/my-repo"

# List the hints found on your machine
wt clue list --source git
```

### Step 2: Inspect and split unbooked time (`wt reconcile`)

At the end of the day, check if any tasks you touched are missing logged hours, preview a split across them, and apply it:

```bash
# 1. Check for tasks with Git hints or completed agenda items that have no logged time
wt reconcile inspect

# 2. Preview how an unallocated block would be split proportionally across two tasks
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
