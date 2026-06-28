# Quickstart: Webex → Microsoft Teams migration

End-to-end guide for running a dry-run migration test from Cisco Webex to
Microsoft Teams. Real Webex data is extracted and a complete Graph-format import
plan is produced. Teams execution is dry-run (no live Graph API calls) until a
live Graph adapter is wired in.

---

## What this covers

| Phase | What happens | Network calls |
|-------|-------------|---------------|
| Extract | Pull conversations, memberships, messages, attachments from Webex API → local archive | Webex REST API |
| Identity map | Generate a JSON template mapping Webex user IDs → Entra UPNs; you fill in the UPNs | None |
| Conversation map | Generate a JSON template mapping Webex rooms → Teams targets; you fill in targets | None |
| Import plan | Prepare all messages in Graph external-import format with deduplicated timestamps | None |
| Execute (dry-run) | Assign synthetic `dry-run:<source-id>` message IDs; write message map | None |
| Verify | Audit that every planned message has a message-map entry | None |

A live Teams import requires a `GraphTeamsAdapter` implementation (not yet built).
The dry-run end-to-end is the validation step before that adapter is added.

---

## Prerequisites

```bash
# Python 3.11+ required; 3.14 recommended
python3.14 --version

# Install the package
pip install -e .

# Verify the CLI works
exodus --version
```

---

## Step 0 — Create your migration config

Copy `examples/webex-to-teams.example.toml` to a working copy and edit it:

```toml
# migration.toml
name = "webex-to-teams"
mode = "individual"
workspace = ".exodus/webex-to-teams"
runtime = "local"

[source]
kind = "webex"
auth = "env:WEBEX_ACCESS_TOKEN"     # personal access token from developer.webex.com
scope = "user_rooms"                # "user_rooms" = all rooms visible to this token
# scope = "selected_rooms"          # uncomment to target specific rooms only
# room_ids = ["Y2lzY29...", "..."] # required when scope = "selected_rooms"
# message_since = "2024-01-01T00:00:00Z"   # optional: lower bound on message date
# message_before = "2025-01-01T00:00:00Z"  # optional: upper bound on message date

[target]
kind = "teams"
# The fields below are stored for future live-execution use.
# They are not used during dry-run.
auth = "graph"
tenant_id = "env:MICROSOFT_TENANT_ID"
client_id = "env:MICROSOFT_CLIENT_ID"
client_secret = "env:MICROSOFT_CLIENT_SECRET"
import_mode = "graph_migration"
```

Get a Webex personal access token from <https://developer.webex.com/docs/getting-started>
(lasts 12 hours; use a bot token or OAuth for longer sessions).

---

## Step 1 — Validate config and secrets

```bash
WEBEX_ACCESS_TOKEN=<your-token> exodus doctor --config migration.toml
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

`Secrets: FAILED` means your token or env var is wrong. Fix that before continuing.

---

## Step 2 — Extract Webex data

```bash
WEBEX_ACCESS_TOKEN=<your-token> exodus export-dry-run \
  --config migration.toml \
  --job-id pilot-export
```

This creates `.exodus/webex-to-teams/archive/` with:
- `conversations/` — one JSONL file per Webex room
- `participants.jsonl` — all unique participants (with emails where visible)
- `memberships.jsonl` — per-room membership records
- `messages/` — per-conversation message archives
- `attachments/` — downloaded file metadata and content

For a large workspace this may take a while (Webex paginates at 50 messages/page).
The job is resumable — re-run with the same `--job-id` to continue after a failure.

---

## Step 3 — Generate identity map template

```bash
exodus teams-identity-map-template \
  --config migration.toml \
  --output identity-map.json
```

This writes `identity-map.json` with one entry per Webex participant discovered
in the archive. Each entry looks like:

```json
{
  "source_user_id": "Y2lzY29zcGFyazovL3VzL1BFT...",
  "source_display_name": "Alice Chen",
  "source_email": "alice@company.webex.com",
  "entra_user_id": ""
}
```

**Fill in `entra_user_id`** with the user's UPN or object ID in your Azure AD /
Entra ID tenant (usually the same as their Teams login email, e.g.
`alice@company.com`). Leave blank or remove entries for participants who should
not be mapped (their messages will fail validation).

Optional shortcut — auto-prefill from an Entra user export:

```bash
# Export users from Entra ID: Azure Portal → Users → Download users (CSV)
exodus teams-identity-map-template \
  --config migration.toml \
  --output identity-map.json \
  --entra-users entra-users.csv
```

This matches Webex emails against Entra UPN / mail / proxyAddresses and
pre-fills exact matches. Review entries marked with `needs_review` comments.

---

## Step 4 — Generate conversation map template

```bash
exodus teams-conversation-map-template \
  --config migration.toml \
  --identity-map identity-map.json \
  --output conversation-map.json
```

This writes `conversation-map.json` with one entry per archived Webex room.
Direct messages → suggested `one_on_one_chat`. Small groups (≤8 members) →
`group_chat`. Larger groups → `team_channel` or `review_required`.

Each entry:

```json
{
  "source_conversation_id": "Y2lzY29zcGFya...",
  "source_title": "Project Alpha",
  "target_kind": "group_chat",
  "target": {
    "members": ["alice@company.com", "bob@company.com"]
  }
}
```

For `team_channel` targets, fill in:

```json
{
  "target_kind": "team_channel",
  "target": {
    "team_id": "<Teams-team-GUID>",
    "channel_id": "<Teams-channel-GUID>"
  }
}
```

Find team/channel GUIDs in Teams Admin Center or via the Graph API:
`GET https://graph.microsoft.com/v1.0/teams` and
`GET https://graph.microsoft.com/v1.0/teams/{team-id}/channels`.

Any room left as `review_required` blocks the import plan step — all entries
must be resolved.

---

## Step 5 — Generate the import plan

```bash
exodus teams-import-plan \
  --config migration.toml \
  --identity-map identity-map.json \
  --conversation-map conversation-map.json \
  --output .exodus/webex-to-teams/archive/plans/teams-import-plan.json
```

This writes a JSON plan with every message prepared for Graph external import:
- `createdDateTime` at millisecond precision (timestamps deduplicated — replies
  shifted forward by 1 ms when collision occurs; capped at import_cutoff)
- Full thread structure preserved (parents before replies)
- `author_user_id` resolved to Entra ID
- Attachment metadata included; unsupported types flagged

The plan is human-readable and auditable before execution.

---

## Step 6 — Execute (dry-run)

```bash
exodus teams-execute-plan \
  --config migration.toml \
  --job-id pilot-teams
```

This runs the plan through `DryRunTeamsAdapter`, which assigns
`dry-run:<source-message-id>` as the Teams message ID for each message and
writes the message map to
`.exodus/webex-to-teams/archive/mappings/teams-message-map.json`.

Expected output:
```
Execution: OK
Message map: .exodus/webex-to-teams/archive/mappings/teams-message-map.json
Messages: 1234/1234
Skipped: 0
```

---

## Step 7 — Verify

```bash
exodus teams-verify-import \
  --config migration.toml
```

This audits that every message in the plan has a message-map entry and that no
extra mappings exist. Writes a JSON verification report.

Expected output:
```
Verification: OK
Report: .exodus/webex-to-teams/archive/reports/teams-import-verification.json
Messages: 1234/1234
Extra mappings: 0
Unsupported attachments: 0
```

Non-zero `Unsupported attachments` is informational — those attachment types
cannot be migrated via Graph external import.

---

## All-in-one command

Once your `identity-map.json` and `conversation-map.json` are filled in you can
run the full pipeline in one command:

```bash
WEBEX_ACCESS_TOKEN=<your-token> exodus webex-teams-dry-run \
  --config migration.toml \
  --identity-map identity-map.json \
  --conversation-map conversation-map.json \
  --job-id pilot
```

This runs extract → plan → execute → verify in sequence, stopping on any error.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Secrets: FAILED — Environment variable is not set for source.auth: WEBEX_ACCESS_TOKEN` | Token not in environment | Export `WEBEX_ACCESS_TOKEN` before running |
| `source.scope selected_rooms requires non-empty source.room_ids` | scope=selected_rooms but no room IDs | Add `room_ids` to config or switch to `user_rooms` |
| `Archive contains no identities to map` | Archive empty — export hasn't run | Run `export-dry-run` first |
| `Teams identity map row N missing entra_user_id` | Unfilled entry in identity map | Fill in all `entra_user_id` values |
| `review_required` entries blocking import plan | Conversation map has unresolved rooms | Fill in `target_kind` and `target` for those rooms |
| `Message N author X is not mapped to Entra ID` | Participant missing from identity map | Add the participant to `identity-map.json` |
| `Teams import job already completed` | Re-running a completed job | Use a different `--job-id` or delete the job store |

---

## What a live Teams import would require

To move from dry-run to live Graph API execution:

1. Register an app in Azure Entra ID with `Teamwork.Migrate.All` (or
   `ChannelMessage.Send` for send-replay) application permission.
2. Grant admin consent for the permission in your tenant.
3. Implement `GraphTeamsAdapter` (satisfies the `TeamsMessageAdapter` Protocol in
   `exodus_agent/targets/teams_executor.py`) using the Microsoft Graph
   [Import third-party platform messages into Teams](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/import-messages/import-external-messages-to-teams)
   API — specifically the migration-mode channel flow.
4. Pass the adapter to `execute_teams_import_plan(..., adapter=GraphTeamsAdapter(...))`.
5. Teams-side: the target teams/channels must be placed in migration mode
   (`POST /teams/{team-id}/completeMigration` only after all messages are imported).

The dry-run end-to-end this quickstart exercises is the correct gate before
building and authorizing the live adapter.
