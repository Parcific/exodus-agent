# Exodus Agent

Omni migration tooling for moving collaboration data between systems.

The first implemented migration paths are Webex chat history to Telegram and
Webex chat history to Microsoft Teams, starting with single-user/local worker
flows and explicit gates for compliance-approved organization exports.

## Product Shape

Exodus Agent starts as a hybrid product with a local-first CLI/worker and an
optional cloud control plane. Migration is mostly deterministic ETL: extract,
normalize, plan, dry-run, execute, verify, and resume. AI is optional and kept out
of the message data path unless explicitly enabled for documentation research or
mapping suggestions.

Why local worker first:

- Lowest security surface: tokens and exports stay on the operator machine.
- Best fit for long-running resumable bulk jobs.
- Easier approval/audit in enterprise environments.
- Fastest path to the Webex -> Telegram and Webex -> Teams use cases.
- Can later expose the same core as an MCP server, desktop app, or cloud worker.

Why optional cloud:

- Better fleet orchestration for many users, tenants, or migration waves.
- Central dashboards, reports, policy templates, and job scheduling.
- Managed connector updates without forcing every customer to upgrade manually.

## Repository Layout

```text
exodus_agent/          Python package and CLI entrypoint
docs/
  architecture.md      System design and execution model
  research/
    webex-telegram.md  Current source-backed research for the first connector pair
  adr/
    0001-tool-format.md
    0002-runtime-model.md
examples/
  webex-to-telegram.example.toml
  webex-to-telegram.organization.example.toml
  webex-to-teams.example.toml
```

## Quickstart

For a step-by-step Webex → Teams migration test see
[docs/quickstart-webex-to-teams.md](docs/quickstart-webex-to-teams.md).

## Installation

### macOS / Linux

```bash
pip install -e .
exodus --help
```

### Windows (no Docker)

Requires Python 3.11+ from [python.org](https://www.python.org/downloads/) with "Add to PATH" checked.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
exodus --help
```

Activate the virtual environment (`.venv\Scripts\activate`) each time you open a new terminal.

### Air-gap / offline transfer

If the target machine has no internet access, build a Docker image on a connected machine
and transfer it via USB:

```bash
# On the internet-connected build machine (macOS or Linux with Docker):
./scripts/bundle.sh
# → dist/exodus-agent-docker.tar.gz  (load with: docker load < exodus-agent-docker.tar.gz)
# → dist/TRANSFER.md                 (step-by-step for the target machine)
```

On **Windows without Docker**: zip the repo source, copy to the target via USB, then follow
the Windows install steps above. Python 3.11+ must already be on the target machine.

## Early CLI

After `pip install -e .` the `exodus` binary is on your PATH.
`python -m exodus_agent` also works without installing.

```bash
exodus --help
exodus plan --config examples/webex-to-telegram.example.toml
exodus doctor --config examples/webex-to-telegram.example.toml
WEBEX_ACCESS_TOKEN=... exodus export-dry-run \
  --config examples/webex-to-telegram.example.toml \
  --job-id pilot
exodus telegram-destination-map-template \
  --config examples/webex-to-telegram.example.toml
exodus telegram-package \
  --config examples/webex-to-telegram.example.toml
exodus telegram-verify \
  --config examples/webex-to-telegram.example.toml
exodus telegram-import-plan \
  --config examples/webex-to-telegram.example.toml \
  --destination-map destination-map.json
exodus telegram-execute-plan \
  --config examples/webex-to-telegram.example.toml
exodus telegram-execute-plan \
  --config examples/webex-to-telegram.example.toml \
  --adapter-command "exodus-telegram-mtproto-runner"
exodus telegram-dry-run-workflow \
  --config examples/webex-to-telegram.example.toml \
  --destination-map destination-map.json
WEBEX_ACCESS_TOKEN=... exodus webex-telegram-dry-run \
  --config examples/webex-to-telegram.example.toml \
  --destination-map destination-map.json
exodus teams-identity-map-template \
  --config examples/webex-to-teams.example.toml
exodus teams-conversation-map-template \
  --config examples/webex-to-teams.example.toml \
  --identity-map .exodus/webex-to-teams/archive/mappings/teams-identity-map.json
exodus teams-import-plan \
  --config examples/webex-to-teams.example.toml \
  --identity-map .exodus/webex-to-teams/archive/mappings/teams-identity-map.json \
  --conversation-map .exodus/webex-to-teams/archive/mappings/teams-conversation-map.json
exodus teams-execute-plan \
  --config examples/webex-to-teams.example.toml
exodus teams-verify-import \
  --config examples/webex-to-teams.example.toml
exodus teams-dry-run-workflow \
  --config examples/webex-to-teams.example.toml \
  --identity-map .exodus/webex-to-teams/archive/mappings/teams-identity-map.json \
  --conversation-map .exodus/webex-to-teams/archive/mappings/teams-conversation-map.json
WEBEX_ACCESS_TOKEN=... exodus webex-teams-dry-run \
  --config examples/webex-to-teams.example.toml \
  --identity-map ./approved-teams-identity-map.json \
  --conversation-map ./approved-teams-conversation-map.json
```

`destination-map.json` is a JSON object mapping source conversation IDs to
Telegram destination peers, for example:

```json
{
  "webex-room-id": "@telegram_archive_group"
}
```

The `telegram-destination-map-template` command writes an annotated template to
`<workspace>/archive/mappings/telegram-destination-map.json` by default. Fill in
each `peer` value with a Telegram username, group, channel, or other Telethon
peer identifier before planning or executing an import.

The current implementation can write a canonical archive from a Webex
individual-mode source and generate Telegram staging packages from that archive.
It can verify package/archive parity, write an audit report, and generate a
fail-closed MTProto import operation plan. It can also dry-run that plan through
the executor boundary or run the full post-export dry-run workflow with auditable
job events. The `webex-telegram-dry-run` command runs the full current Webex to
Telegram dry-run path in one command. Webex file attachments are stored under the
archive with SHA-256 metadata so Telegram media upload planning can fail closed
when local media is missing. A subprocess adapter can consume one MTProto
operation as stdin JSON and return log-safe JSON metadata on stdout; adapter
results are redacted before entering job logs.

For Teams, the current implementation can generate strict identity and
conversation mapping templates, write a Graph-ready dry-run import plan, execute
that plan through an idempotent message-map boundary, and verify the message map
against the plan. Teams `createdDateTime` values are emitted at millisecond
precision; original source timestamps remain in audit fields. Timestamp
collisions and replies whose source timestamp is earlier than the parent are
deterministically shifted forward and recorded in the plan.

Webex extraction supports an optional bounded message window with
`source.message_since` and `source.message_before` ISO-8601 datetimes. The
`message_before` value is sent to Webex as an API query parameter and both
bounds are enforced locally before messages enter the archive.

The bundled `exodus-telegram-mtproto-runner` is currently a fail-closed runner
boundary by default: it validates supported MTProto import operations and
required environment, then refuses live execution unless explicitly enabled.
Install the optional Telegram dependency before live execution:

```bash
python -m pip install ".[telegram]"
```

The runner expects `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`,
and explicit `EXODUS_TELEGRAM_LIVE=1`. With the optional dependency installed it
maps the operation plan to Telethon MTProto requests for `checkHistoryImport`,
`checkHistoryImportPeer`, `initHistoryImport`, `uploadImportedMedia`, and
`startHistoryImport`. Keep using dry-run execution until destination peers,
operator permissions, and a small pilot archive have been verified.

Organization mode has an explicit preflight policy gate. A config with
`mode = "organization"` must use `source.scope = "organization"` and include a
`[policy]` section with `legal_basis`, `approved_by`, `retention_start`,
`retention_end`, and `include_direct_messages`. Actual Webex organization export
still requires the future compliance/admin extractor; the current Webex extractor
supports individual user rooms and selected rooms only.

## First Migration Strategy

Preferred path:

1. Extract Webex spaces, memberships, messages, attachments, edits, deletes, and
   source metadata into a canonical local archive.
2. Convert each Webex room into a Telegram-compatible import package.
3. Use Telegram's MTProto history import flow where allowed.
4. Verify message/media counts and produce an audit report.

Fallback path:

Use Telegram Bot API or TDLib send-message replay only when history import is not
allowed. This is less faithful because messages are newly sent by the bot/account
and cannot fully preserve original authorship/timestamps.
