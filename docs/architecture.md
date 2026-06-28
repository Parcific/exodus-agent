# Exodus Agent Architecture

## Goals

- Move chat history and attachments between collaboration systems.
- Preserve source metadata, authorship labels, timestamps, threading, and media
  whenever the destination allows it.
- Support individual and organization-scale runs.
- Be resumable, auditable, and safe to dry-run.
- Use AI only where it improves speed without weakening security or correctness.

## Non-Goals

- Bypassing platform permissions, retention policies, or user consent.
- Silently replaying messages as real users.
- Making an LLM responsible for irreversible writes.

## Recommended Shape

Use a hybrid architecture with a local-first worker backed by a reusable migration
engine.

Runtime surfaces:

- CLI: first-class operator surface for local and CI-driven migrations.
- Local worker: long-running resumable executor, controlled by CLI or cloud.
- Cloud control plane: optional dashboards, scheduling, policy, fleet management,
  and reports. It should not require storing customer message bodies.
- MCP server: useful for letting other agents inspect migration plans, docs, and
  dry-run reports.
- Desktop app: useful after connector behavior is stable.
- Hosted SaaS workers: only after enterprise auth, tenant isolation, and
  compliance requirements are fully designed.

## Execution Model

```text
Source Connector -> Canonical Archive -> Planner -> Target Session -> Verifier
                      |                    |
                      v                    v
                 Resumable Job Store   Audit Report
```

### Phases

1. Preflight: validate credentials, scopes, destination permissions, rate limits,
   storage space, and migration policy.
2. Extract: fetch rooms, members, messages, files, edits, deletes, reactions, and
   source IDs into append-only raw records.
3. Normalize: convert raw records into a canonical model.
4. Transform: map source rooms to destination chats and source users to display
   identities.
5. Load: import history or replay messages.
6. Verify: compare counts, timestamps, media checksums, and known failures.
7. Report: write human-readable and machine-readable migration reports.

This mirrors the proven Teams import pattern: analyze and prepare source data,
map source containers and identities to destination structure, start a
destination migration/import session, import messages, complete/finalize the
session, then verify completion.

## Extensible Connector Model

Every connector implements one or more capabilities instead of a single rigid
interface:

- `DiscoverySource`: list workspaces, users, conversations, and membership.
- `MessageSource`: page historical messages and replies.
- `MediaSource`: download or stream attachments.
- `IdentityDirectory`: resolve users, groups, aliases, and deleted accounts.
- `DestinationProvisioner`: create or bind destination containers.
- `MigrationSessionTarget`: start/check/complete destination migration mode.
- `HistoricalImportTarget`: write messages with original timestamps/authors when
  the platform supports it.
- `ReplayTarget`: send messages as a bot or migration account when historical
  import is unavailable.
- `Verifier`: compare imported destination state against the canonical archive.

Connectors declare capabilities and constraints in metadata so the planner can
choose the safest route automatically.

## Open Formats

Exodus should store every migration in a portable archive:

```text
archive/
  manifest.json
  conversations/*.jsonl
  participants/*.jsonl
  memberships/*.jsonl
  messages/{conversation_id}.jsonl
  attachments/{sha256-prefix}/{sha256}
  mappings/
    identities.csv
    conversations.csv
  reports/
```

The archive is the contract between extractors and loaders. This keeps future
workflows open: Webex -> Telegram, Webex -> Teams, Slack -> Teams, Teams ->
Telegram, Discord -> Matrix, and archive-only exports can reuse the same engine.

## Canonical Model

Core entities:

- `Workspace`: source tenant or user scope.
- `Conversation`: Webex room, Telegram group, channel, or private chat.
- `Participant`: source user identity plus destination mapping.
- `Message`: stable source ID, author, timestamp, text/markdown/html, thread
  parent, edits, deletes, reactions, and attachments.
- `Attachment`: source URL, filename, MIME type, size, checksum, local path.
- `MigrationEvent`: append-only operation log for retries and audit.

## Webex -> Telegram Strategy

### Individual Mode

Use a Webex user token to enumerate accessible rooms and messages. Export each
room into a canonical archive, then create Telegram import packages or load into
operator-selected Telegram destinations.

### Organization Mode

Use a Webex compliance/admin-approved identity. The tool must enforce explicit
policy configuration for retention window, users/spaces in scope, and consent or
legal basis. Destination mapping should default to one Telegram supergroup per
Webex space, with imported messages labeled by original source author.

### Telegram Load Modes

Preferred: MTProto history import.

- Best preservation of historical timestamps/media.
- Destination eligibility must be checked per peer.
- Requires user/admin confirmation and appropriate Telegram rights.

Fallback: bot/account replay.

- Simpler API path.
- Does not faithfully preserve original authorship/timestamps.
- Should be reserved for small migrations or cases where import is blocked.

## AI Policy

Default: no AI in the migration hot path.

Allowed uses:

- API documentation discovery for new connectors.
- Schema/mapping suggestions before operator approval.
- Classification of unsupported content after redaction.

Disallowed by default:

- Sending message bodies or attachments to an LLM.
- Allowing an LLM to execute destination writes without deterministic validation.
- Using browser automation to bypass official APIs.

## Security

- Read credentials from environment variables or OS keychain, not config files.
- Encrypt local archives when they contain message bodies.
- Store raw and normalized records separately for audit.
- Redact secrets in logs.
- Use least-privilege platform scopes.
- Require dry-run before destructive or high-volume writes.
- Add per-connector rate-limiters and exponential backoff.

## Local vs Cloud

Recommended default: local worker with optional cloud control plane.

Run local when:

- Migrating sensitive messages or attachments.
- The customer cannot allow third-party custody of admin tokens.
- The job is a pilot, one-off, or small/medium organization migration.
- Data residency is unclear.

Use cloud orchestration when:

- Many tenants, users, or migration waves need centralized scheduling.
- Operators need dashboards, approvals, and durable reporting.
- Connector updates and policy templates matter more than a single-machine
  workflow.

Avoid cloud-hosted data plane until the product has mature tenant isolation,
encryption, deletion guarantees, audit logs, abuse controls, and compliance
processes.

## Implementation Roadmap

1. Define canonical archive schema and resumable job store.
2. Implement connector capability contracts and registry.
3. Implement Webex extractor for individual mode.
4. Implement Telegram import-package writer and MTProto import adapter.
5. Add verifier and audit reports.
6. Add Webex -> Teams as a second target to prove connector generality.
7. Add organization mode behind stricter preflight policy checks.
8. Add optional MCP/cloud control-plane surfaces for plan/report inspection.
