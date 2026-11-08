# Web Cockpit, Shell Prompts, AI & Team Hub

Beyond the `wt` command line, Worktrack includes four optional ways to interact with your schedule and team: a keyboard-driven **Web Cockpit**, **Shell Prompt plugins**, a local **AI Assistant connector (`wt mcp`)**, and **Team Hub sync (`wt hub`)**.

______________________________________________________________________

## 1. The Web Cockpit (`wt serve`)

### Why use it?

When you are reviewing your week, splitting several unallocated time blocks, or inspecting Outbox previews before sending hours to Jira, a visual dashboard is often faster and clearer than running individual terminal commands.

### How to start it

Start the local web server on your computer:

```bash
wt serve --host 127.0.0.1 --port 8000
```

If you are running from a source checkout, you can also start the live frontend UI in a second terminal with `pnpm -C frontend run dev`.

### Keyboard Shortcuts

You can navigate the entire web app without touching your mouse:

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

## 2. Shell Prompt Integration (`wt daemon` & `shell/`)

### Why use it?

When your current task and elapsed time (`[PROJ-142 0:42]`) appear right in your terminal prompt, you always know whether a timer is running and never forget to clock out when switching tasks.

### How to set it up

Because your shell draws a new prompt after every command you type, starting a fresh Python process every time would make your terminal feel slow. Worktrack includes a tiny background helper (`wt daemon`) that keeps your current status ready in memory so your prompt updates in a few milliseconds:

```bash
# Start the background helper for your session
wt daemon --background
```

Then add the snippet for your shell from the `shell/` directory:

- **`starship.rs` (`shell/starship.toml`)**:
  Copy the `[custom.worktrack]` block from `shell/starship.toml` into `$XDG_CONFIG_HOME/starship.toml`.
- **`oh-my-zsh` (`shell/worktrack.plugin.zsh`)**:
  Source `shell/worktrack.plugin.zsh` in `$ZDOTDIR/.zshrc` and add `$(worktrack_prompt_info)` to `PROMPT` or `RPROMPT`.
- **PowerShell (`shell/worktrack.ps1`)**:
  Dot-source `shell/worktrack.ps1` in `$PROFILE` and call `Get-WorktrackPromptSegment` inside your `prompt` function.

> **Optional setting:** If you ever need to change where the background helper stores its local socket file, set `WORKTRACK_SOCKET_PATH` (defaults to `$XDG_RUNTIME_DIR/worktrack/worktrackd.sock`).

______________________________________________________________________

## 3. Connecting Local AI Assistants (`wt mcp`)

### Why use it?

If you use an AI assistant or editor that supports the **Model Context Protocol (MCP)**, you can connect it to Worktrack to ask natural-language questions like *"How much time did I spend on PROJ-142 this week?"* or *"Propose a time split for my afternoon block based on my Git commits"*.

### How it protects your privacy

The MCP server allows the AI assistant to read your local schedule and stage draft proposals into your **Outbox**, but it **blocks any attempt to send data directly to Jira, your calendar, or a Team Hub**. You always review and approve Outbox items yourself.

```bash
# Start the MCP server over standard input/output for your AI client
wt mcp
```

______________________________________________________________________

## 4. Syncing with a Shared Team Hub (`wt hub`)

### Why use it?

If you work on a team that shares a common list of projects, customers, and cost centers, setting up those names on every person's laptop by hand is repetitive. A shared **Team Hub** lets your team:

1. Pull a shared catalog of projects, customers, and default attribution weights (`wt hub pull-catalog`).
1. Optionally share approved, rounded time rollups via the Outbox — without ever uploading anyone's raw daily timeline or personal notes.

```bash
# Check local and shared Hub status
wt hub status

# Pull your team's shared project and customer catalog
wt hub pull-catalog --hub-url https://hub.example.invalid
```
