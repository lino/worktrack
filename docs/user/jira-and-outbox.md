# Jira Cloud Login, Sync & Outbox

This guide covers connecting Worktrack to **Jira Cloud** so you can pull your assigned issues into your local task list and send rounded worklogs back to Jira when you are ready.

______________________________________________________________________

## Why Use the Jira Connector & Outbox?

If your team tracks work in Jira, copying hours from your notes into Jira every Friday is tedious. At the same time, you probably don't want every 7-minute timer start/stop or personal scratch note posted straight to a shared ticket.

Worktrack solves both problems:

1. **Pull your assigned Jira issues** (`wt jira sync`) so you can start tracking time against them immediately (`wt in PROJ-142`).
1. **Stage and review in your Outbox first** (`wt outbox`): Worktrack groups and rounds your hours locally so you can preview the exact totals and approve what gets sent before anything touches Jira.

Your Jira credentials are stored in your operating system's native password manager (**macOS Keychain**, **Linux Secret Service / KWallet**, or **Windows Credential Manager**) — never in plain-text config files.

______________________________________________________________________

## Step 1: Set Your Jira Cloud Site ID

Tell Worktrack which Jira Cloud site you use by setting `WORKTRACK_JIRA_CLOUD_ID` in your shell profile:

```bash
export WORKTRACK_JIRA_CLOUD_ID="your-atlassian-cloud-id"
```

> **Tip — How to find your Jira Cloud ID:**
> Open `https://<your-company>.atlassian.net/_edge/tenant_info` in your browser while signed in to Jira. It returns a small JSON snippet like `{"cloudId": "01234567-89ab-cdef-0123-456789abcdef"}`.

______________________________________________________________________

## Step 2: Log In to Jira (`wt jira login`)

You can sign in either via an OAuth 2.0 browser flow or by saving an access token directly.

### Option A: Browser Login (OAuth 2.0)

1. Generate your login link:

   ```bash
   export WORKTRACK_JIRA_CLIENT_ID="your-oauth-client-id"
   export WORKTRACK_JIRA_REDIRECT_URI="http://127.0.0.1:5173/callback"

   wt jira auth-url --json
   ```

1. Open the `authorization_url` from the output in your browser and click **Accept** on the Atlassian screen.

1. Copy the `code` parameter from the redirect URL and complete the login:

   ```bash
   wt jira login --code "<code-from-redirect>" --code-verifier "<code_verifier>"
   ```

### Option B: Direct Token Login

If you already have a Jira OAuth 2.0 access token (and optional refresh token), save it directly into your computer's keychain:

```bash
wt jira login \
  --access-token "<jira-oauth-access-token>" \
  --refresh-token "<optional-refresh-token>"
```

### Check Login Status or Sign Out

```bash
# Check whether you are signed in (never prints your secret token)
wt jira status

# Sign out and remove your stored credentials from the keychain
wt jira logout
```

______________________________________________________________________

## Step 3: Pull Your Assigned Issues (`wt jira sync`)

Import your open Jira issues so you can track time against them by issue key:

```bash
# Sync open issues assigned to you
wt jira sync

# Or pass a custom JQL filter
wt jira sync --jql 'assignee = currentUser() AND statusCategory != Done'
```

______________________________________________________________________

## Step 4: Preview, Approve & Send Worklogs (`wt outbox`)

When you are ready to log your hours to Jira (for example, at the end of the day or week):

```bash
# 1. Group and round your tracked time into the Outbox for review
wt outbox preview --sink jira --stage --cadence eod

# 2. Inspect what is waiting in your Outbox and approve the items you want to send
wt outbox list
wt outbox approve <outbox-item-id>

# 3. Send approved worklogs to Jira Cloud
wt outbox flush --sink jira

# 4. View a local receipt log of everything you have sent
wt outbox ledger
```

If an item in your Outbox doesn't look right, you can reject it with `wt outbox reject <outbox-item-id>`, adjust your local time blocks, and run `wt outbox preview --sink jira --stage` again.

______________________________________________________________________

## Jira Configuration Reference

| What It Controls                  | Environment Variable          | Default                                               |
| --------------------------------- | ----------------------------- | ----------------------------------------------------- |
| Your Atlassian Cloud site ID      | `WORKTRACK_JIRA_CLOUD_ID`     | *(required when using Atlassian Cloud)*               |
| Custom Jira API base URL override | `WORKTRACK_JIRA_API_BASE_URL` | `https://api.atlassian.com/ex/jira/<cloud_id>`        |
| Default JQL filter for `sync`     | `WORKTRACK_JIRA_JQL`          | `assignee = currentUser() AND statusCategory != Done` |
| OAuth 2.0 app `client_id`         | `WORKTRACK_JIRA_CLIENT_ID`    | *(used by `wt jira auth-url`)*                        |
| OAuth 2.0 `redirect_uri`          | `WORKTRACK_JIRA_REDIRECT_URI` | *(used by `wt jira auth-url` and `wt jira login`)*    |
