# MCP Server for AI Assistants (`wt mcp`)

This guide explains **why** you might want to connect a local AI assistant (such as Claude Desktop, Cursor, Windsurf, or Antigravity) to Worktrack via the **Model Context Protocol (MCP)**, **how** it protects your privacy, and **how** to set it up.

______________________________________________________________________

## Why Use the MCP Server?

If you already work alongside an AI coding assistant or desktop chat app, switching windows just to check how much time you have left on today's budget or how to divide an afternoon block across tickets adds friction.

With `wt mcp`, you can ask your AI assistant in plain language:

- *"What am I currently clocked into, and how much time have I logged today?"*
- *"What's on my agenda for today and this week?"*
- *"Look at my recent Git hints and propose how to split my 2-hour afternoon block between PROJ-142 and PROJ-100."*
- *"Stage my end-of-day worklogs into the Outbox so I can review them."*

______________________________________________________________________

## How It Protects Your Privacy & Prevents Accidents

Giving an AI assistant direct write access to your company's Jira or work calendar is risky — a misunderstood prompt could post unfinished notes or wrong hours to a client ticket.

Worktrack's MCP server enforces a strict boundary:

1. **Runs 100% locally over standard input/output (`stdio`)**: No third-party cloud relay sits between your AI client and your local Worktrack database.
1. **Read & Draft Only — Never Sends Externally**: The MCP server can read your local schedule and **stage draft proposals into your Outbox** (`status = "staged"`), but any attempt by an AI tool call to flush the Outbox or write directly to Jira, your calendar, or a Team Hub is automatically blocked. You always have the final say before anything leaves your computer.

______________________________________________________________________

## How to Configure Your AI Client

Add `worktrack` to your MCP client configuration file (for example, `claude_desktop_config.json` or `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "worktrack": {
      "command": "wt",
      "args": ["mcp"]
    }
  }
}
```

If you store your Worktrack database in a custom location instead of the default folder, pass `WORKTRACK_DB_PATH` in the `env` block:

```json
{
  "mcpServers": {
    "worktrack": {
      "command": "wt",
      "args": ["mcp"],
      "env": {
        "WORKTRACK_DB_PATH": "$XDG_DATA_HOME/worktrack/ledger.sqlite3"
      }
    }
  }
}
```

### Testing from the terminal

You can also test a single JSON-RPC request from the command line using `--once`:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n' | wt mcp --once
```

______________________________________________________________________

## Available MCP Tools

Once connected, your AI assistant can use these six tools:

| Tool Name                       | What It Does                                                                                     | Can It Send Data Externally? |
| ------------------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------- |
| `worktrack_status`              | Checks your active timer, elapsed time, today's total hours, and today's work location           | No (Read-only)               |
| `worktrack_insights`            | Summarizes your time breakdown and project shares for `now`, `today`, or `this_week`             | No (Read-only)               |
| `worktrack_agenda`              | Lists your planned tasks (`today`, `this_week`, `backlog`) and scheduled Focus Blocks            | No (Read-only)               |
| `worktrack_clues`               | Reads local Git commit, pull request, and calendar hints to help you remember what you worked on | No (Read-only)               |
| `worktrack_propose_retrosplit`  | Previews how an unallocated time block would be split across a list of tasks                     | No (Preview-only)            |
| `worktrack_stage_outbox_rollup` | Rounds and stages worklog proposals into your local **Outbox** (`staged`) for your manual review | No (Stages in Outbox only)   |
