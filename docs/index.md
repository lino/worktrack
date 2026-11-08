# Welcome to Worktrack (`wt`)

**Worktrack (`wt`)** helps you keep track of your work hours, plan your day and week, and log time to tools like Jira — without turning your computer into a surveillance device.

Most time trackers force a frustrating choice: either you manually start and stop timers every time you switch tasks, or you install "automatic" activity monitors that upload every window title and keystroke to a cloud server.

Worktrack takes a different approach:

1. **You own your raw timeline**: Your exact start/stop times, personal scratch notes, and local activity hints stay on your computer.
1. **You can track live or fill in the blanks later**: Clock in and out as you work, or log a rough afternoon block and split it across tasks at the end of the day using hints from your Git commits and calendar meetings.
1. **Nothing leaves your computer without your approval**: Before any hours are sent to Jira, your calendar, or a team server, Worktrack groups and rounds them in your **Outbox** so you can preview and approve exactly what gets shared.

______________________________________________________________________

## Documentation by Component

So you only have to read about the features you actually want to use, the documentation is split into focused guides:

### User Guides

- **[Quickstart & Tracking Time](user/guide.md)**: Start and stop timers, log past blocks of time, and see where your day and week went.
- **[Planning, Focus Time & Reconciling Your Day](user/planning-and-reconciliation.md)**: Plan *Today* and *This Week*, block out uninterrupted focus time, and use local Git hints to split unbooked hours.
- **[Jira Cloud Login, Sync & Outbox](user/jira-and-outbox.md)**: Log in to Jira Cloud, pull your assigned issues, and preview/approve rounded worklogs before sending them.
- **[Calendars, Work Location & Reports](user/calendars-and-reports.md)**: Import calendar meetings, track `home_office` / `office` / `mobile_work` days, link tasks to projects or customers, and export annual tax or project reports.
- **[Web Cockpit, Shell Prompts, AI & Team Hub](user/cockpit-and-integrations.md)**: Use the keyboard-driven web interface, show your active timer in your shell prompt, connect a local AI assistant (`wt mcp`), or pull shared project lists from a Team Hub (`wt hub`).

### Developer & Self-Hosting Guides

- **[Architecture & Contributing](developer/guide.md)**: How Worktrack works under the hood, core domain concepts, and how to build and run the test suite from source.
- **[Metrics Host, Health Checks & Docker](developer/metrics-and-monitoring.md)**: Why and when you might want to run the built-in Prometheus metrics and health server (for example, when self-hosting a shared Team Hub or Docker container) and how to configure it.
