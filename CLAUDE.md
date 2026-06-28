# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Exodus Agent is a **local-first deterministic ETL migration tool** for moving collaboration data between platforms. Primary scope: Webex chat history → Telegram and Webex chat history → Microsoft Teams. AI assistance is optional and explicitly kept out of the message data path.

## Commands

```bash
# Install (base)
pip install -e .

# Install with Telegram MTProto support
pip install -e ".[telegram]"

# Run CLI
python -m exodus_agent --help
python -m exodus_agent plan --config examples/webex-to-telegram.example.toml

# Run all tests
python3 -m unittest discover -s tests

# Run a single test file
python3 -m unittest tests.test_cli
python3 -m unittest tests.test_runner

# Run a single test case
python3 -m unittest tests.test_cli.TestCLI.test_plan_command

# Syntax check
python3 -m compileall exodus_agent tests
```

## Architecture

### Data Flow

```
Source → Archive → Planner → Target Session → Verifier → Report
```

1. **Source** (`exodus_agent/sources/`) extracts from Webex API — pagination, retry, rate-limit handling.
2. **Archive** (`exodus_agent/archive.py`) writes to a canonical JSONL workspace under the configured `workspace` path: `conversations.jsonl`, `participants.jsonl`, `messages.jsonl`, attachments with SHA-256 integrity. The archive is protocol-agnostic.
3. **Planner** (`exodus_agent/planner.py`) validates config and determines execution strategy.
4. **Target** (`exodus_agent/targets/`) maps source → destination and executes. Telegram uses a staging package + MTProto subprocess adapter boundary. Teams uses Graph API identity/conversation mapping templates.
5. **Verifier** compares destination state against archive for audit-ready JSON/CSV output.

### Key Module Roles

| Module | Role |
|--------|------|
| `cli.py` | Entry point; subcommands: `plan`, `export`, `import-plan`, `run` |
| `model.py` | Core domain types: `Workspace`, `Conversation`, `Participant`, `Message`, `Attachment` |
| `protocols.py` | Interfaces: `DiscoverySource`, `MessageSource`, `MediaSource`, `HistoricalImportTarget` |
| `config.py` | TOML config loading; modes (`individual`/`organization`), runtimes (`local`/`cloud`) |
| `job.py` | Append-only `JobStore` for resumable execution with `JobEvent` records |
| `runner.py` | `export_dry_run()` orchestrates source extraction into canonical archive |
| `workflow.py` | High-level workflow orchestrators composing runner + target operations |
| `connectors.py` | Connector registry and factory |
| `archive.py` | Reads and writes the canonical JSONL archive format |
| `mtproto_runner.py` | Standalone fail-closed subprocess adapter for Telethon (keeps MTProto isolated) |
| `sources/webex.py` | Webex REST API client (~800 lines) |
| `targets/telegram.py` | Staging package creation, destination maps, import plan generation |
| `targets/telegram_executor.py` | MTProto operation execution via subprocess boundary |
| `targets/teams_mapping.py` | Identity/conversation mapping templates, timestamp collision handling |
| `targets/teams_executor.py` | Teams Graph API import execution and verification |

### Configuration Format (TOML)

See `examples/` for full examples. Key top-level fields:

```toml
name = "migration-name"
mode = "individual"          # individual | organization
workspace = ".exodus/..."    # local path for archive + job store
runtime = "local"            # local | customer_cloud_worker | managed_cloud_worker

[source]
kind = "webex"
auth = "env:WEBEX_ACCESS_TOKEN"

[target]
kind = "telegram"  # or "teams"
# Telegram: auth = "mtproto", api_id, api_hash, session
# Teams: auth = "graph", tenant_id, client_id, client_secret

[policy]  # required for organization mode
legal_basis = "..."
approved_by = "..."
```

Secrets use the `env:VAR_NAME` notation and are resolved at runtime via `secrets.py`.

### Adapter Boundaries

- **Telegram MTProto** runs in a subprocess (`mtproto_runner.py`) to isolate the Telethon dependency. The main process communicates via stdin/stdout JSON protocol.
- **Teams Graph API** calls run in-process via `teams_executor.py`.

### Known Issue (from HANDOFF.md)

`cli.py` line ~738 has a missing closing parenthesis on a `_run_cli_action()` call in the `webex-teams-dry-run` command handler. This prevents the module from importing. Fix before running tests.

## Test Structure

Tests use Python's `unittest` (no pytest). 16 test files mirroring the module structure in `tests/`. The HANDOFF.md notes the last verified baseline was 351 passing tests.

Major test files:
- `test_teams_mapping.py` — identity/conversation mapping, import plans, timestamp collision
- `test_teams_executor.py` — Graph API execution, idempotency, verification
- `test_webex_source.py` — pagination, rate-limiting, extraction
- `test_telegram_target.py` — staging package creation, verification
- `test_cli.py` — CLI command routing, template generation
- `test_runner.py` — export dry-run, archive writing, attachment materialization
- `test_workflow.py` — end-to-end workflow orchestration

## Architecture Decisions (ADRs in `docs/`)

- **ADR 0001**: Local-first CLI/worker — operators keep tokens, exports, and logs under local control.
- **ADR 0002**: Hybrid runtime — local data plane for connectors, optional cloud control plane for orchestration.

AI policy: allowed for documentation research and mapping suggestions; disallowed in the message data hot path.
