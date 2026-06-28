# ADR 0002: Hybrid Runtime, Local Data Plane First

## Status

Accepted.

## Decision

Exodus Agent should use a hybrid runtime:

- Local data plane: connector workers run near the customer's data and secrets.
- Optional cloud control plane: orchestration, dashboards, policy, templates,
  connector registry, billing, and non-sensitive telemetry.

The first implementation is the local CLI/worker. Cloud is added after the core
engine, archive schema, and first connector are stable.

## Rationale

Chat migration handles sensitive message history, attachments, identity maps, and
admin credentials. Running the data plane locally or inside the customer's cloud
keeps high-risk data under customer control and simplifies early trust.

Pure cloud is better for non-technical onboarding and very large managed
migrations, but it increases compliance burden immediately: tenant isolation,
data residency, encryption, deletion, audit, SOC 2 style controls, incident
response, and connector secret custody.

Pure local is easiest to trust and ship, but it is weaker for many migration waves
because operators lose centralized progress, retries, reporting, and policy.

## Consequences

- Default product: local CLI/worker.
- Enterprise product: cloud control plane with bring-your-own-worker.
- Future managed product: hosted workers only after compliance hardening.
- The engine must avoid assumptions about where it runs.

