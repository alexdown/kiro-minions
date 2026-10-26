# Architecture Decision Registry (ADR)

This registry records architecture decisions for kiro-minions. Each ADR captures
one decision: its context, the options considered, the tradeoffs, and (once you
decide) the chosen option and its consequences.

**How to use this during review:** every ADR below is currently
`PROPOSED` with the recommendation called out. Read the ADR, pick an option (or
propose another), and change its **Status** to `ACCEPTED` with a one-line
rationale. A fresh agent session will then implement against the accepted set.

## Format

Each ADR follows: **Status · Context · Options (with tradeoffs) · Decision ·
Consequences**. Status is one of `PROPOSED` / `ACCEPTED` / `REJECTED` /
`SUPERSEDED`.

## Index — decisions needed for the IDE ingress feature

| ADR | Decision | Status | Recommendation |
|-----|----------|--------|----------------|
| [ADR-001](ADR-001-developer-authentication.md) | How the IDE / developer authenticates to the ingress | PROPOSED | OIDC via the customer's existing IdP |
| [ADR-002](ADR-002-task-shape.md) | Task shape for v1: scoped vs open-ended | PROPOSED | **A — scoped tasks** |
| [ADR-003](ADR-003-results-channel.md) | Results channel: polling vs streaming | PROPOSED | **A — polling** for v1 |
| [ADR-004](ADR-004-idempotency-and-schema.md) | Idempotency key & DynamoDB schema | PROPOSED | Generic `task_id`, additive schema |
| [ADR-005](ADR-005-payload-naming.md) | Payload contract naming / generalisation | PROPOSED | Generalise to `TaskPayload`, keep field names |
| [ADR-006](ADR-006-result-delivery.md) | How the result reaches the dev: PR / branch / patch | PROPOSED | **A — PR** |
| [ADR-007](ADR-007-concurrency-and-cost-guardrails.md) | Concurrency, quota & cost guardrails for scaled parallelism | PROPOSED | Reserved concurrency + per-actor quota + budget alarms |

See [../ide-ingress.md](../ide-ingress.md) for the spec these decisions support.
