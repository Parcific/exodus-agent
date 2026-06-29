# Quickstart: Webex → Microsoft Teams migration

Move your Cisco Webex chat history — conversations, messages, and participants —
into Microsoft Teams. This guide walks you through every step from setup to
verification, with both a **dry-run** mode (nothing is sent to Teams, safe to
test any time) and a **live** mode (messages are actually posted to Teams).

> **Safe to use at any point.** The tool only reads from Webex and only adds
> messages to Teams. It never deletes, edits, or modifies any existing data.
> If it fails or is interrupted mid-run, re-run with the same `--job-id` to
> resume exactly where it left off. Nothing is left in a corrupt state.

---

## Before you start

You will need:

| What | Where to get it | Required for |
|------|----------------|-------------|
| **Webex personal access token** | [developer.webex.com](https://developer.webex.com/docs/getting-started) → click "Copy" under your name | Extract step |
| **Microsoft Azure app credentials** (3 values: tenant ID, client ID, client secret) | Azure portal → App registrations | Live Teams posting only |
| **Python 3.11 or later** | [python.org/downloads](https://www.python.org/downloads/) | Always |

The Webex token expires after 12 hours. For longer migrations, create a **bot token** or use OAuth — both are explained at developer.webex.com.

You can run the full dry-run (Steps 1–7) with only the Webex token. You only need the Azure credentials if you want to actually post messages into Teams (Step 6 Live).

---

## Install the tool

**macOS / Linux** — open Terminal:
```bash
pip install -e .
exodus --version   # should print 0.1.0
```

**Windows** — open PowerShell:
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
exodus --version   # should print 0.1.0
```

Run `.venv\Scripts\activate` each time you open a new PowerShell window before using `exodus`.

---

## Pilot run (recommended — do this before the full migration)

Test the full end-to-end flow on a small, disposable slice of data before
touching your real chats. This lets you confirm credentials work, messages
look right in Teams, and the tool behaves as expected — with zero risk to
your real data.

### What you need for the pilot

- **One Webex room** with a handful of messages (a 1-on-1 or a small project room)
- **A narrow date window** — e.g. the last two weeks
- **Two throwaway Teams targets** — one group chat and one 1-on-1 chat created
  specifically for the test, both deleted afterward

### Create the throwaway Teams targets

**Throwaway group chat:**
1. Open Teams → click the **New chat** icon (pencil at the top)
2. Type your own name and at least one colleague → name the chat **"Migration test — delete me"**
3. Open the chat, copy the ID from the browser URL:
   `https://teams.microsoft.com/l/chat/19:abc123@thread.v2/...`
   → `19:abc123@thread.v2` is your `chat_id`

**Throwaway 1-on-1 chat:**
1. Open Teams → click **New chat** → type just one colleague's name (someone
   who does not mind a few test messages appearing)
2. Send a quick message: *"Testing migration tool — I'll clean this up"*
3. Copy the chat ID from the browser URL the same way as above

> If you have a dedicated test/dev Microsoft 365 account in your org, use that
> as the colleague — the test messages land there and no real person is affected.

### Create a scoped pilot config

Save this as `migration-pilot.toml` — edit the room ID and date window:

```toml
name = "pilot"
mode = "individual"
workspace = ".exodus/pilot"
runtime = "local"

[source]
kind = "webex"
auth = "env:WEBEX_ACCESS_TOKEN"
scope = "selected_rooms"
room_ids = ["Y2lzY29zcGFy..."]        # paste the Webex room ID here
message_since = "2025-06-01T00:00:00Z" # limit to a small window
message_before = "2025-07-01T00:00:00Z"

[target]
kind = "teams"
auth = "graph"
tenant_id = "env:MICROSOFT_TENANT_ID"
client_id = "env:MICROSOFT_CLIENT_ID"
client_secret = "env:MICROSOFT_CLIENT_SECRET"
```

> **How to find a Webex room ID:** run the full extract once (`exodus export-dry-run`)
> and open `.exodus/.../archive/conversations.jsonl` — the `source_id` field on
> each line is the room ID.

### Create minimal identity and conversation maps

`identity-map-pilot.json` — only the people in that room (fill in their Teams email):
```json
[
  {
    "source_user_id": "Y2lzY29zcGFy...",
    "display_name": "Alice Chen",
    "email": "alice@company.webex.com",
    "entra_user_id": "alice@yourcompany.com"
  }
]
```

`conversation-map-pilot.json` — map the Webex room to both throwaway chats
(use the `source_conversation_id` from the extracted archive):

```json
[
  {
    "source_conversation_id": "Y2lzY29zcGFy...",
    "title": "My test room (group)",
    "target_kind": "group_chat",
    "target": {
      "chat_id": "19:abc123@thread.v2"
    }
  },
  {
    "source_conversation_id": "Y2lzY29zcGFy...",
    "title": "My test room (1-on-1)",
    "target_kind": "one_on_one_chat",
    "target": {
      "chat_id": "19:xyz789@thread.v2"
    }
  }
]
```

> You can point the same Webex room at both target types to test both in one run.

### Run the pilot

**macOS / Linux:**
```bash
export WEBEX_ACCESS_TOKEN="..."
export MICROSOFT_TENANT_ID="..."
export MICROSOFT_CLIENT_ID="..."
export MICROSOFT_CLIENT_SECRET="..."

exodus webex-teams-dry-run \
  --config migration-pilot.toml \
  --identity-map identity-map-pilot.json \
  --conversation-map conversation-map-pilot.json \
  --job-id pilot-1
```

**Windows (PowerShell):**
```powershell
$env:WEBEX_ACCESS_TOKEN      = "..."
$env:MICROSOFT_TENANT_ID     = "..."
$env:MICROSOFT_CLIENT_ID     = "..."
$env:MICROSOFT_CLIENT_SECRET = "..."

exodus webex-teams-dry-run `
  --config migration-pilot.toml `
  --identity-map identity-map-pilot.json `
  --conversation-map conversation-map-pilot.json `
  --job-id pilot-1
```

Expected output:
```
Adapter: GraphTeamsAdapter
Workflow: OK
Messages: 12/12
Verification: OK
```

### Verify in Teams

Open both throwaway chats in Teams. You should see the Webex messages posted,
each showing the original author and send time:

> *Migrated from Webex | Originally sent: 2025-06-15T09:32:00Z*
>
> The original message text here.

Check that:
- All expected messages appear in both chats
- Names and timestamps in the provenance header look correct
- The message count matches what `Verification: OK` reported

### Clean up

Delete both throwaway Teams chats (right-click the chat → **Delete**).
Your real chats are untouched. Delete `.exodus/pilot/` if you want a clean slate.

Once the pilot looks good, proceed with the full migration below using your
real `migration.toml`, `identity-map.json`, and `conversation-map.json`.

---

## Step 1 — Create your config file

Copy the example and save it as `migration.toml` in your working folder:

```toml
name = "webex-to-teams"
mode = "individual"
workspace = ".exodus/webex-to-teams"
runtime = "local"

[source]
kind = "webex"
auth = "env:WEBEX_ACCESS_TOKEN"
scope = "user_rooms"
# scope = "selected_rooms"          # uncomment to migrate specific rooms only
# room_ids = ["Y2lzY29...", "..."] # paste room IDs here when using selected_rooms
# message_since = "2024-01-01T00:00:00Z"   # optional: only messages after this date
# message_before = "2025-01-01T00:00:00Z"  # optional: only messages before this date

[target]
kind = "teams"
auth = "graph"
tenant_id = "env:MICROSOFT_TENANT_ID"
client_id = "env:MICROSOFT_CLIENT_ID"
client_secret = "env:MICROSOFT_CLIENT_SECRET"
```

The `env:` prefix means the value is read from an environment variable — your
credentials never touch the config file itself.

---

## Step 2 — Validate your setup

Set your Webex token and check that everything is wired correctly:

**macOS / Linux:**
```bash
export WEBEX_ACCESS_TOKEN="paste-your-token-here"
exodus doctor --config migration.toml
```

**Windows (PowerShell):**
```powershell
$env:WEBEX_ACCESS_TOKEN = "paste-your-token-here"
exodus doctor --config migration.toml
```

Expected output:
```
Config: OK
Migration: webex -> teams
Mode: individual
Runtime: local
Workspace: .exodus/webex-to-teams
Secrets: OK
```

`Secrets: FAILED` means the token variable is not set or the name is mistyped. Fix it before continuing.

---

## Step 3 — Extract your Webex data

This downloads your conversations, messages, and attachments from Webex into a
local archive. Nothing is sent to Teams yet.

**macOS / Linux:**
```bash
exodus export-dry-run --config migration.toml --job-id pilot-export
```

**Windows (PowerShell):**
```powershell
exodus export-dry-run --config migration.toml --job-id pilot-export
```

The archive is saved to `.exodus/webex-to-teams/archive/`. For large workspaces
this may take several minutes — Webex returns 50 messages per page. If it
stops for any reason, re-run the same command to resume.

---

## Step 4 — Map Webex users to Teams users

Generate a template that lists every Webex participant found in your archive:

```bash
exodus teams-identity-map-template \
  --config migration.toml \
  --output identity-map.json
```

Open `identity-map.json` and fill in the `entra_user_id` field for each person.
This is their **Microsoft 365 login email** (e.g. `alice@yourcompany.com`) — the
same address they use to sign in to Teams.

```json
{
  "source_user_id": "Y2lzY29zcGFyazov...",
  "display_name": "Alice Chen",
  "email": "alice@company.webex.com",
  "entra_user_id": "alice@yourcompany.com"
}
```

**Shortcut — auto-fill from an Azure user export:**
1. Open [Azure portal](https://portal.azure.com) → **Users** → **Download users** → choose CSV
2. Run:
   ```bash
   exodus teams-identity-map-template \
     --config migration.toml \
     --output identity-map.json \
     --entra-users entra-users.csv
   ```
   Matches are filled in automatically. Review entries marked `needs_review`.

---

## Step 5 — Map Webex rooms to Teams destinations

Generate a template that lists every archived Webex room:

```bash
exodus teams-conversation-map-template \
  --config migration.toml \
  --identity-map identity-map.json \
  --output conversation-map.json
```

Open `conversation-map.json`. For each room, fill in where in Teams the messages
should land. There are two target types:

**Group chat or 1-on-1 chat** — fill in the Teams chat ID:
```json
{
  "source_conversation_id": "Y2lzY29zcGFya...",
  "title": "Project Alpha",
  "target_kind": "group_chat",
  "target": {
    "chat_id": "19:abc123@thread.v2"
  }
}
```

**Team channel** — fill in the team ID and channel ID:
```json
{
  "target_kind": "team_channel",
  "target": {
    "team_id": "00000000-1111-2222-3333-444444444444",
    "channel_id": "19:abc@thread.skype"
  }
}
```

**How to find chat IDs and channel IDs:**
- **Chat ID**: Open the Teams web app, go to the chat, copy the ID from the URL
  (`https://teams.microsoft.com/l/chat/<chat-id>/...`)
- **Channel ID**: Open the channel → `...` menu → Get link to channel → the long
  string after `/channel/` is the channel ID
- Or ask your IT admin to export these from the Teams Admin Center

Any room still set to `review_required` will block Step 6 — all rooms must be resolved.

---

## Step 6 — Generate the import plan

This prepares every message for posting to Teams:

```bash
exodus teams-import-plan \
  --config migration.toml \
  --identity-map identity-map.json \
  --conversation-map conversation-map.json
```

The plan is saved as a readable JSON file you can inspect before executing.
Thread structure, timestamps, and author IDs are all resolved at this stage.

---

## Step 7 — Execute

### Dry-run (safe — nothing is sent to Teams)

Remove or leave empty the three `env:MICROSOFT_*` variables in your config,
then run:

```bash
exodus teams-execute-plan --config migration.toml --job-id pilot-teams
```

Expected output:
```
Adapter: DryRunTeamsAdapter
Execution: OK
Message map: .exodus/webex-to-teams/archive/mappings/teams-message-map.json
Messages: 1234/1234
Skipped: 0
```

`Adapter: DryRunTeamsAdapter` confirms no real API calls were made.

### Live — actually post messages to Teams

Set your Azure credentials, then run the same command:

**macOS / Linux:**
```bash
export MICROSOFT_TENANT_ID="your-tenant-id"
export MICROSOFT_CLIENT_ID="your-client-id"
export MICROSOFT_CLIENT_SECRET="your-client-secret"
exodus teams-execute-plan --config migration.toml --job-id live-run-1
```

**Windows (PowerShell):**
```powershell
$env:MICROSOFT_TENANT_ID     = "your-tenant-id"
$env:MICROSOFT_CLIENT_ID     = "your-client-id"
$env:MICROSOFT_CLIENT_SECRET = "your-client-secret"
exodus teams-execute-plan --config migration.toml --job-id live-run-1
```

Expected output:
```
Adapter: GraphTeamsAdapter
Execution: OK
Messages: 1234/1234
Skipped: 0
```

`Adapter: GraphTeamsAdapter` confirms messages are being posted to Teams for real.

**What the migrated messages look like in Teams:**
Each message appears from the registered Azure app (not the original user).
The original author and send time are shown in the message body:

> *Migrated from Webex | Originally sent: 2024-03-15T09:32:00Z*
>
> Hi team, the report is ready.

**Azure app setup** (do this once, ask your IT admin if unsure):
1. Go to [Azure portal](https://portal.azure.com) → **App registrations** → **New registration**
2. Name it (e.g. "Exodus Migration"), leave defaults, click **Register**
3. Copy the **Application (client) ID** → this is your `client_id`
4. Copy the **Directory (tenant) ID** → this is your `tenant_id`
5. Go to **Certificates & secrets** → **New client secret** → copy the value → this is your `client_secret`
6. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**
   - Add `Chat.ReadWrite.All` (for group chats and 1-on-1 chats)
   - Add `ChannelMessage.Send` (for team channels)
7. Click **Grant admin consent** (requires admin role)

---

## Step 8 — Verify

Check that every planned message has a corresponding Teams message ID:

```bash
exodus teams-verify-import --config migration.toml
```

Expected output:
```
Verification: OK
Report: .exodus/webex-to-teams/archive/reports/teams-import-verification.json
Messages: 1234/1234
Extra mappings: 0
Unsupported attachments: 3
```

`Unsupported attachments` is informational — some file types cannot be migrated
via the Teams API. The text content of those messages is still migrated.

---

## All-in-one command

Once your maps are filled in, run the full pipeline (extract → plan → execute → verify)
in one command:

**macOS / Linux:**
```bash
exodus webex-teams-dry-run \
  --config migration.toml \
  --identity-map identity-map.json \
  --conversation-map conversation-map.json \
  --job-id pilot
```

**Windows (PowerShell):**
```powershell
exodus webex-teams-dry-run `
  --config migration.toml `
  --identity-map identity-map.json `
  --conversation-map conversation-map.json `
  --job-id pilot
```

With Azure credentials set, this runs a full live migration. Without them, it runs dry-run.

---

## Troubleshooting

| What you see | What it means | How to fix it |
|---|---|---|
| `Secrets: FAILED — Environment variable is not set for source.auth: WEBEX_ACCESS_TOKEN` | The `WEBEX_ACCESS_TOKEN` variable is not set in your terminal | Run `export WEBEX_ACCESS_TOKEN="..."` (Mac/Linux) or `$env:WEBEX_ACCESS_TOKEN = "..."` (Windows PowerShell) |
| `source.scope selected_rooms requires non-empty source.room_ids` | You set `scope = "selected_rooms"` but forgot to list the rooms | Add the room IDs under `room_ids` in your config, or change scope back to `user_rooms` |
| `Archive contains no identities to map` | The archive is empty — extraction has not run yet | Run Step 3 (export-dry-run) first |
| `Teams identity map row N missing entra_user_id` | One or more people in `identity-map.json` have an empty `entra_user_id` | Open `identity-map.json` and fill in the Microsoft 365 email for each person |
| `review_required` entries blocking import plan | Some rooms in `conversation-map.json` are not yet assigned a Teams target | Open `conversation-map.json` and fill in `target_kind` and `target` for those rooms |
| `Teams import job already completed` | You are re-running a job that already finished | Use a different `--job-id` (e.g. `--job-id run-2`) |
| `GraphApiError: Microsoft OAuth2 token request failed` | The Azure credentials are wrong or the app permission was not granted | Double-check tenant ID, client ID, client secret; make sure admin consent was granted in Azure |
| `GraphApiError: Graph API request failed: status=403` | The Azure app is missing the required API permission | Go to Azure → App registrations → API permissions → add `Chat.ReadWrite.All` or `ChannelMessage.Send`, then grant admin consent |
| `Partial Graph credentials in [target]: missing client_secret` | You set some but not all three Azure credentials | Either set all three (`tenant_id`, `client_id`, `client_secret`) or set none (dry-run) |
