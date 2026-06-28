# ADR 0001: Start as a Local-First CLI

## Status

Accepted.

## Decision

Exodus Agent starts as a local-first CLI with a reusable core library. It may
later expose the same core through MCP, a desktop app, or hosted workers.

## Rationale

Migration work is credential-heavy, long-running, and audit-sensitive. A CLI lets
operators keep source exports, destination sessions, and logs under local control.
It also makes resumability and dry-runs straightforward.

Agentic AI is not the default execution engine. The default migration path is
deterministic scripts and typed connectors. AI can help research unfamiliar APIs,
draft mapping plans, or classify unsupported content, but only behind explicit
operator approval and redaction controls.

## Consequences

- Fastest secure path for Webex -> Telegram.
- Easy CI testing for connector logic.
- Less convenient than a GUI for non-technical users.
- Future UX can be layered over the same job engine.

