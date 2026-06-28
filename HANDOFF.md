# Exodus Agent — Handoff (2026-06-29)

This document is the authoritative handoff for the next agent (Codex or Claude Code) picking up
this codebase. Read it top to bottom before touching anything.

---

## Project Purpose

Local-first deterministic ETL tool for migrating collaboration data between platforms.
Primary scope: **Webex → Microsoft Teams** chat history. No cloud dependency; the operator
runs this on their own machine, keeping tokens, exports, and logs under local control.

Repo: `https://github.com/Parcific/exodus-agent`
Language: Python 3.11+
Test runner: `python3 -m unittest discover -s tests` (stdlib only, no pytest)
Entry point: `exodus` CLI (installed via `pip install -e .`)

---

## Current State (post-session 2026-06-29)

### Tests

```
Ran 414 tests in ~2s — ALL PASSING
```

### Commits landed this session (oldest to newest, all on origin/main)

```
b44bf03  docs(quickstart): fix wrong JSON field names and python version check
6634f60  docs(readme): complete Teams command migration to exodus binary
e702b40  docs(claude-md): update Commands section to exodus binary
a52a3a3  fix(webex): correct grammar for singular attempt count in retry errors
4e2e7be  refactor(webex): extract shared _request_with_backoff helper
73aab2d  feat(teams): implement GraphTeamsAdapter with OAuth2 and retry logic
792a491  fix(teams): url-encode graph api ids, guard oauth non-json, fix html double-wrap
52bf3e8  fix(teams): handle URLError retries, html injection, 401 exhaustion, retry-after zero
5011302  fix(cli): wire adapter to teams-dry-run-workflow, resolve all graph creds, error on partial
bd92e1b  refactor(workflow): eliminate duplicate teams import completion predicate  <- HEAD
```

Code-review workflow w5ruqk5tv found 10 confirmed bugs; all 10 are fixed and pushed.
428 tests — all green.

---

## Architecture (what matters for next steps)

### Data flow

```
Source (Webex) -> Archive (JSONL) -> Planner -> Import Plan -> Executor -> Verifier
```

### Key files for Teams migration

| File | Role |
|------|------|
| `exodus_agent/targets/graph_teams_adapter.py` | NEW — live Graph API adapter |
| `exodus_agent/targets/teams_executor.py` | Executes import plan; `TeamsMessageAdapter` Protocol; `DryRunTeamsAdapter` |
| `exodus_agent/targets/teams_mapping.py` | Builds identity map, conversation map, import plan |
| `exodus_agent/workflow.py` | High-level workflow orchestrators (both accept optional `adapter` param) |
| `exodus_agent/cli.py` | CLI entry point; `_teams_adapter_from_config()` auto-selects adapter |
| `tests/test_graph_teams_adapter.py` | NEW — 40 unit tests for GraphTeamsAdapter |

### GraphTeamsAdapter (just shipped in commit 73aab2d)

`exodus_agent/targets/graph_teams_adapter.py`

**`_TokenCache`** — client-credentials OAuth2 token, refreshed transparently:
- Fetches from `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`
- Caches for the token lifetime minus 60 s margin
- `invalidate()` forces refresh on next `get()` — called when Graph returns 401

**`GraphTeamsAdapter`** — implements `TeamsMessageAdapter` Protocol:
- `import_message(message)` returns `{"teams_message_id": "...", "graph_created_date_time": "..."}`
- URL built from `target_kind`:
  - `group_chat` / `one_on_one_chat` → `POST /chats/{chatId}/messages`
  - `team_channel` → `POST /teams/{teamId}/channels/{channelId}/messages`
- Embeds migration provenance in HTML body (original Webex timestamp) because `createdDateTime`
  is read-only without `Teamwork.Migrate.All` app permission
- Retries 429 (Retry-After header), re-fetches token on 401, raises `GraphApiError` on exhaustion
- Injectable transports: `graph_transport` and `oauth_transport` kwargs for testing

**Config-driven activation** (`_teams_adapter_from_config` in `cli.py`):
- If `[target]` section has `tenant_id` + `client_id` + `client_secret` → `GraphTeamsAdapter`
- Otherwise → `DryRunTeamsAdapter` (no network calls, returns fake IDs)
- Both `teams-execute-plan` and `webex-teams-dry-run` print `Adapter: <ClassName>` at startup

### TOML config for live Teams import

```toml
[target]
kind = "teams"
auth = "graph"
tenant_id = "env:MICROSOFT_TENANT_ID"
client_id = "env:MICROSOFT_CLIENT_ID"
client_secret = "env:MICROSOFT_CLIENT_SECRET"
```

Required Azure AD app permissions:
- `Chat.ReadWrite.All` — for group_chat / one_on_one_chat
- `ChannelMessage.Send` — for team_channel

### Why historical timestamps are not preserved

Microsoft Graph only allows `createdDateTime` to be set with `Teamwork.Migrate.All` permission,
which requires Teams migration mode (the channel/chat must be freshly created in migration mode).
We target EXISTING chats/channels (user fills `chat_id`/`team_id`/`channel_id` in the
conversation map), so migration mode cannot be used. The original timestamp is embedded as
`<em>Originally sent: {original_created_at}</em>` in the HTML body instead.

### Why messages appear from the app, not the original user

`Chat.ReadWrite.All` app-only permission does not allow setting `from.user` to a specific
Entra user — Azure AD controls that field and maps it to the registered application. To truly
impersonate senders you would need delegated permissions per-user (interactive login), which
is incompatible with a batch CLI tool.

---

## In-Flight Work (must complete before bundling)

### 1. Docker bundle (no blockers remaining)

```bash
./scripts/bundle.sh
# Output: dist/exodus-agent-docker.tar.gz + dist/TRANSFER.md
```

`Dockerfile` and `scripts/bundle.sh` are already committed. The Dockerfile uses
`python:3.13-slim`, non-root `exodus` user, `/workspace` volume, `ENTRYPOINT ["exodus"]`.

Air-gap transfer: `docker save -> gzip -> USB -> docker load` on target machine.

---

## End-to-End Live Test Steps (for founder after bundle ships)

Prerequisites on test PC:
- Docker with loaded image
- Env vars: `WEBEX_ACCESS_TOKEN`, `MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`

```bash
# 1. Validate config
docker run --rm -e WEBEX_ACCESS_TOKEN=... \
  -v $(pwd):/workspace exodus-agent \
  doctor --config /workspace/migration.toml

# 2. Extract Webex history
docker run --rm -e WEBEX_ACCESS_TOKEN=... \
  -v $(pwd):/workspace exodus-agent \
  export-dry-run --config /workspace/migration.toml

# 3. Identity map template (one entry per Webex user)
docker run --rm -v $(pwd):/workspace exodus-agent \
  teams-identity-map-template \
  --config /workspace/migration.toml \
  --output /workspace/identity-map.json
# -> Fill in entra_user_id for each participant

# 4. Conversation map template (one entry per Webex room)
docker run --rm -v $(pwd):/workspace exodus-agent \
  teams-conversation-map-template \
  --config /workspace/migration.toml \
  --identity-map /workspace/identity-map.json \
  --output /workspace/conversation-map.json
# -> Fill in chat_id (group/1:1) or team_id+channel_id for each room

# 5. Generate import plan
docker run --rm -v $(pwd):/workspace exodus-agent \
  teams-import-plan \
  --config /workspace/migration.toml \
  --identity-map /workspace/identity-map.json \
  --conversation-map /workspace/conversation-map.json

# 6. Live import (GraphTeamsAdapter fires when creds present)
docker run --rm \
  -e MICROSOFT_TENANT_ID=... \
  -e MICROSOFT_CLIENT_ID=... \
  -e MICROSOFT_CLIENT_SECRET=... \
  -v $(pwd):/workspace exodus-agent \
  teams-execute-plan --config /workspace/migration.toml
# Prints: Adapter: GraphTeamsAdapter

# 7. Verify
docker run --rm -v $(pwd):/workspace exodus-agent \
  teams-verify-import --config /workspace/migration.toml
```

---

## Test Commands

```bash
# Full suite (414 tests expected)
/opt/homebrew/bin/python3.14 -m unittest discover -s tests

# Specific files
/opt/homebrew/bin/python3.14 -m unittest tests.test_graph_teams_adapter
/opt/homebrew/bin/python3.14 -m unittest tests.test_cli
/opt/homebrew/bin/python3.14 -m unittest tests.test_teams_executor

# Syntax check
/opt/homebrew/bin/python3.14 -m compileall exodus_agent tests
```

---

## Hard Constraints (do not violate)

- No `git add -A` or `git add .` — stage files specifically
- No `Co-Authored-By` footer in commit messages
- No `--no-verify` flag
- One WP per commit; push after each
- Do not commit `dist/` artifacts (already in `.gitignore`)
- Do not bundle before code-review findings are fixed

---

## Open Questions (surface to founder after review + bundle)

1. **Message attribution**: messages appear from the registered app identity in Teams.
   Should the original Webex author name also be embedded in the HTML body?
   (Currently only timestamp is shown.)
2. **Attachment upload**: `supported=False` attachments are tracked but not uploaded.
   Graph API `driveItem` upload is the natural next WP after the live test passes.
3. **Reply threading**: `parent_source_message_id` is tracked in the plan but the Graph POST
   does not set `replyToId` — replies land flat. `team_channel` supports reply threading via
   `replyToId`; `group_chat` does not. Worth adding for channel targets.
