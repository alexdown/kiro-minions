# Architecture Decision Registry (ADR)

This registry records architecture decisions for kiro-minions. Each ADR captures
one decision: its context, the options considered, the tradeoffs, and (once you
decide) the chosen option and its consequences.

**Status:** every ADR below is now `ACCEPTED` with its recommended option
(the maintainer took the recommended choice for each) and has been implemented
on the `feat/ide-ingress-spec` branch.

## Format

Each ADR follows: **Status · Context · Options (with tradeoffs) · Decision ·
Consequences**. Status is one of `PROPOSED` / `ACCEPTED` / `REJECTED` /
`SUPERSEDED`.

## Index — decisions needed for the IDE ingress feature

| ADR | Decision | Status | Recommendation |
|-----|----------|--------|----------------|
| [ADR-001](ADR-001-developer-authentication.md) | How the IDE / developer authenticates to the ingress | ACCEPTED | OIDC via the customer's existing IdP |
| [ADR-002](ADR-002-task-shape.md) | Task shape for v1: scoped vs open-ended | ACCEPTED | **A — scoped tasks** |
| [ADR-003](ADR-003-results-channel.md) | Results channel: polling vs streaming | ACCEPTED | **A — polling** for v1 |
| [ADR-004](ADR-004-idempotency-and-schema.md) | Idempotency key & DynamoDB schema | ACCEPTED | Generic `task_id`, additive schema |
| [ADR-005](ADR-005-payload-naming.md) | Payload contract naming / generalisation | ACCEPTED | Generalise to `TaskPayload`, keep field names |
| [ADR-006](ADR-006-result-delivery.md) | How the result reaches the dev: PR / branch / patch | ACCEPTED | **A — PR** |
| [ADR-007](ADR-007-concurrency-and-cost-guardrails.md) | Concurrency, quota & cost guardrails for scaled parallelism | ACCEPTED | **A+B+C+D (alert-only)+E** — reserved concurrency + worker cap via SQS backpressure + per-actor quota + budget alarms (alert-only, hard cap deferred) + audit log |

See [../ide-ingress.md](../ide-ingress.md) for the spec these decisions support.
