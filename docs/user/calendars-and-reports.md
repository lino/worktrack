# Calendars, Work Location & Reports

This guide explains how to connect your work calendar, keep track of where you worked each day (`home_office`, `office`, or `mobile_work`) for your annual tax return, link tasks across multiple projects or customers, and export clean reports.

______________________________________________________________________

## 1. Syncing Your Calendar (`wt calendar`)

### Why use it?

Meetings can easily eat up half a day without you realizing where the time went, and deep work requires defending your calendar from new meeting invites. Worktrack lets you:

- **Import meetings** from an `.ics` calendar export, Fastmail (JMAP), or Microsoft 365 so your meeting time is accounted for.
- **Publish Focus Blocks** back to your calendar as simple `"Focus Time"` busy slots — without exposing your private task notes to coworkers.

### How to use it

```bash
# Import meetings from a calendar file (.ics), JMAP, or Microsoft 365
wt calendar sync --ics-file "$XDG_DATA_HOME/worktrack/cal.ics"

# Stage scheduled Focus Blocks into your Outbox to be published as "Focus Time"
wt calendar stage-focus --start 2026-11-08T00:00:00Z --end 2026-11-09T00:00:00Z
```

Once staged, approve and send your calendar blocks through your Outbox (`wt outbox list`, `wt outbox approve <id>`, `wt outbox flush --sink calendar`) just like Jira worklogs.

______________________________________________________________________

## 2. Tracking Where You Worked for Tax Returns (`wt fact` & `wt sensor`)

### Why use it?

Many countries allow you to deduct home-office days on your annual tax return if you have a reliable daily record of when you worked from home (`home_office`), at the company office (`office`), or on the go (`mobile_work`). Worktrack keeps this log strictly on your computer — it is never shared with Jira or your calendar.

### Option A: Log your work location manually (`wt fact`)

```bash
wt fact set workplace home_office --date 2026-11-08
```

### Option B: Detect your work location automatically (`wt sensor`)

If you work from predictable networks or monitor setups (for example, your home Wi-Fi name vs. the office network subnet or an active VPN), Worktrack can check those local rules and record today's work location for you:

```bash
wt sensor evaluate --date 2026-11-08 --apply
```

### Custom daily tags

You can also create your own custom tags if you want to track extra context (such as billing tiers):

```bash
wt fact register-facet billable_rate_tier --type enum --values standard, overtime, research
wt fact set billable_rate_tier research --date 2026-11-08
```

______________________________________________________________________

## 3. Grouping Tasks by Project or Customer (`wt entity` & `wt attr`)

### Why use it?

In real life, a single task often supports more than one project, customer, epic, or cost center (for example, a shared platform fix that benefits Customer A 60% and Customer B 40%). Worktrack lets you link tasks to one or more projects or customers with custom weights so your reports reflect reality.

### How to use it

```bash
# Register a customer or project
wt entity add customer ACME --name "Acme Corp"
wt entity add customer GLOBEX --name "Globex"

# Attribute task PROJ-142 60% to Acme and 40% to Globex
wt attr add --activity PROJ-142 --target customer:acme --weight 0.6
wt attr add --activity PROJ-142 --target customer:globex --weight 0.4
```

______________________________________________________________________

## 4. Exporting Tax & Project Reports (`wt export`)

### Why use it?

At tax time or at the end of a billing cycle, you need a clean document you can hand to your accountant, attach to an invoice, or open in a spreadsheet.

### How to use it

Export in `Markdown`, `CSV`, or `JSON`:

```bash
# Annual home-office / office / mobile-work summary for your tax return
wt export tax --year 2026 --format markdown

# Project, customer, and cost-center time breakdown for a date range
wt export attribution \
  --start 2026-11-01T00:00:00Z \
  --end 2026-11-08T00:00:00Z \
  --format csv
```

For jurisdiction-specific tax deduction rules (such as Germany's Homeoffice-Pauschale and Entfernungspauschale), vacation and public holiday exclusions, and business travel allowances (*Reisekosten* & *Verpflegungsmehraufwand*), see the dedicated [Tax Packs, Travel (Reisekosten) & Vacation (Urlaub)](reporting-travel-and-vacation.md) guide.
