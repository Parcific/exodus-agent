# Webex to Teams Mapping Design

## Goal

Map Webex contacts, spaces, and groups to Microsoft Teams targets without
guessing. Exodus should generate deterministic suggestions, require review for
ambiguous cases, and only import after identity and conversation maps are
complete.

## Source Evidence

- Microsoft Graph Teams import supports third-party historical messages through
  migration mode for chats and channels.
- Imported messages can preserve custom timestamps and conversation hierarchy.
- `Teamwork.Migrate.All` is the baseline permission for migration APIs.
- The same app that starts a migration session must import and complete it.
- Message `createdDateTime` values must be unique in the same thread.
- Microsoft documents five imported messages per second per channel.
- Webex extraction should use official rooms, memberships, people, messages, and
  compliance/admin event APIs where scope requires them.

Primary references:

- https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/import-messages/import-external-messages-to-teams
- https://learn.microsoft.com/en-us/graph/api/chat-post
- https://developer.webex.com/messaging/docs/api/v1/messages/list-messages
- https://developer.webex.com/messaging/docs/api/v1/memberships/list-memberships
- https://developer.webex.com/admin/docs/api/v1/events/list-events

## Required Maps

### Identity Map

`webex_person_id -> entra_user_id`

Match order:

1. Exact Webex email to Entra `mail`.
2. Exact Webex email to Entra `userPrincipalName`.
3. Approved alias or proxy address.
4. Manual operator mapping.

Display-name-only matches must never be accepted automatically.

The implementation writes an `exodus.teams.identity_map.v1` JSON template with
one row per Webex participant:

```json
{
  "format": "exodus.teams.identity_map.v1",
  "identities": [
    {
      "source_user_id": "webex-person-id",
      "entra_user_id": "",
      "display_name": "Ada Lovelace",
      "email": "ada@example.com",
      "status": "needs_review",
      "reason": "Provide Microsoft Entra user ID."
    }
  ]
}
```

Conversation mapping must load a completed identity map. Empty `entra_user_id`
values are rejected.

CLI workflow:

```bash
python -m exodus_agent teams-identity-map-template \
  --config examples/webex-to-teams.example.toml

# Optional: prefill exact email matches from a local Microsoft Graph users export.
python -m exodus_agent teams-identity-map-template \
  --config examples/webex-to-teams.example.toml \
  --entra-users ./entra-users.json \
  --overwrite

# Fill every entra_user_id in:
# .exodus/webex-to-teams/archive/mappings/teams-identity-map.json

python -m exodus_agent teams-conversation-map-template \
  --config examples/webex-to-teams.example.toml \
  --identity-map .exodus/webex-to-teams/archive/mappings/teams-identity-map.json

# Fill every conversation target in:
# .exodus/webex-to-teams/archive/mappings/teams-conversation-map.json

python -m exodus_agent teams-import-plan \
  --config examples/webex-to-teams.example.toml \
  --identity-map .exodus/webex-to-teams/archive/mappings/teams-identity-map.json \
  --conversation-map .exodus/webex-to-teams/archive/mappings/teams-conversation-map.json

python -m exodus_agent teams-execute-plan \
  --config examples/webex-to-teams.example.toml

python -m exodus_agent teams-verify-import \
  --config examples/webex-to-teams.example.toml

# Or run plan + dry-run execute + verify in one local workflow:
python -m exodus_agent teams-dry-run-workflow \
  --config examples/webex-to-teams.example.toml \
  --identity-map .exodus/webex-to-teams/archive/mappings/teams-identity-map.json \
  --conversation-map .exodus/webex-to-teams/archive/mappings/teams-conversation-map.json

# Or extract Webex first, then run the Teams dry-run workflow:
python -m exodus_agent webex-teams-dry-run \
  --config examples/webex-to-teams.example.toml \
  --identity-map ./approved-teams-identity-map.json \
  --conversation-map ./approved-teams-conversation-map.json
```

`webex-teams-dry-run` loads the approved identity and conversation map files
before resetting the archive for a fresh Webex export. Keep approved maps outside
the archive workspace when using that command. Existing external message-map
files are refused for this fresh-export workflow to avoid silently reusing stale
destination IDs; use `teams-dry-run-workflow` when intentionally resuming from an
existing archive and message map.

The optional `--entra-users` input can be a Microsoft Graph JSON export shaped as
an array or `{ "value": [...] }`, or a CSV with `id`, `mail`,
`userPrincipalName`, and optional `proxyAddresses`/`otherMails` columns. Exodus
only pre-fills exact Webex email matches against Entra `mail`,
`userPrincipalName`, proxy addresses, or other mails. Ambiguous email matches and
display-name-only matches remain `needs_review`.

### Conversation Map

`webex_room_id -> teams_target`

Targets:

- `one_on_one_chat`
- `group_chat`
- `team_channel`
- `review_required`

Default classification:

- Webex direct conversation with exactly two mapped users -> one-on-one chat.
- Small Webex space/group with all users mapped -> group chat.
- Larger or persistent Webex space -> Teams channel.
- Missing identities, deleted users, external users, or unclear membership ->
  review required.

Current implementation note: the deterministic classifier uses canonical
per-conversation membership rows when present. For older archives without
membership rows, it falls back to observed message authors and should be treated
as lower confidence before live provisioning.

The conversation-map template includes an operator-filled `target` object. A
completed map is strict:

- `one_on_one_chat` and `group_chat` rows require `target.chat_id`.
- `team_channel` rows require `target.team_id` and `target.channel_id`.
- `review_required` rows block import planning until resolved.
- Duplicate source conversations and duplicate Teams target assignments are
  rejected.

Example completed rows:

```json
{
  "format": "exodus.teams.mapping_template.v1",
  "conversations": [
    {
      "source_conversation_id": "webex-room-id-1",
      "target_kind": "group_chat",
      "target": {
        "chat_id": "teams-chat-id"
      }
    },
    {
      "source_conversation_id": "webex-room-id-2",
      "target_kind": "team_channel",
      "target": {
        "team_id": "teams-team-id",
        "channel_id": "teams-channel-id"
      }
    }
  ]
}
```

### Message Map

`webex_message_id -> teams_chat_message_id`

This map is mandatory for idempotency, reply import, retries, and verification.

The current implementation writes an `exodus.teams.import_plan.v1` JSON artifact
with Graph-ready `createdDateTime` values and audit fields for the source
timestamp. Plan rows include `import_order`; parent messages are emitted before
their replies even when source timestamps arrive out of order. When a reply's
source timestamp is before or equal to its parent after millisecond
normalization, the reply `createdDateTime` is moved to the next millisecond after
the parent while `original_created_at`, `timestamp_adjustment_ms`, and
`timestamp_adjustment_reason` preserve the source timing. Missing parents,
duplicate source message IDs, and parent cycles are rejected before any live
Graph execution.

Message attachments are listed on each plan row and repeated in the top-level
`unsupported_attachments` array until a live Graph attachment strategy is
implemented. This keeps media loss visible in reports instead of silently
dropping files.

The dry-run executor writes an `exodus.teams.message_map.v1` artifact under
`archive/mappings/teams-message-map.json`. Live Graph execution must use the same
artifact as its idempotency boundary and write `webex_message_id ->
teams_chat_message_id` as each import operation succeeds.

The verifier writes `archive/reports/teams-import-verification.json` in
`exodus.teams.import_verification.v1` format. It checks that every planned source
message has exactly one mapped Teams message ID and that the message map does not
contain unplanned rows. It also carries the unsupported attachment count and
attachment rows from the import plan, so the final report remains the single
place to inspect known media fidelity gaps. Remote Graph-state verification
should extend this report with destination counts and migration-mode completion
status before a live job is closed.

## Import Flow

1. Extract Webex data into the canonical archive.
2. Resolve identities into Entra IDs.
3. Generate a conversation mapping template.
4. Require operator review for every `review_required` row and any low-confidence
   suggestion.
5. Provision or bind target Teams chats/channels.
6. Start migration mode on the target chat/channel.
7. Import parent messages before replies using the generated `import_order`.
8. Import replies after parent messages.
9. Complete migration only after all import operations pass.
10. Verify migration mode is completed and compare source/destination counts.

## Safety Rules

- Never merge multiple Webex rooms into one Teams target unless an explicit
  approved merge plan exists.
- Never create a Teams message for an unmapped author as if it came from another
  user.
- If timestamps collide in one thread, add deterministic millisecond offsets and
  record the original timestamp in the audit report.
- Store source timestamps as timezone-aware UTC instants in the archive and use
  them as Teams `createdDateTime` values. Microsoft Graph imports at
  millisecond precision, so sub-millisecond source precision must be normalized
  deterministically.
- For group chats, verify `visibleHistoryStartDateTime` after import so members
  can see imported historical messages older than their current share-history
  boundary.
- Respect Graph throttling and the documented per-channel import rate.
- Do not complete a migration session if imports or verification fail.
- Attachments that cannot be imported faithfully must be linked or reported, not
  silently dropped.
