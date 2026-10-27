# Architecture Decision Registry (ADR)

This registry records architecture decisions for kiro-minions. Each ADR captures
one decision: its context, the options considered, the tradeoffs, and (once you
decide) the chosen option and its consequences.

**Status:** ADR-001…007 are `ACCEPTED` and implemented on the
`feat/ide-ingress-spec` branch (ADR-002 as Option C — the spec-executor).
**ADR-008 is `PROPOSED`** (verification gates) and awaits the maintainer's
decisions before any implementation — it is a docs-only proposal.

## Format

Each ADR follows: **Status · Context · Options (with tradeoffs) · Decision ·
Consequences**. Status is one of `PROPOSED` / `ACCEPTED` / `REJECTED` /
`SUPERSEDED`.

## Index — decisions needed for the IDE ingress feature

| ADR | Decision | Status | Recommendation |
|-----|----------|--------|----------------|
| [ADR-001](ADR-001-developer-authentication.md) | How the IDE / developer authenticates to the ingress | ACCEPTED | OIDC via the customer's existing IdP |
| [ADR-002](ADR-002-task-shape.md) | Task shape for v1: scoped vs open-ended | ACCEPTED | **C — new "spec-executor" worker** (reads a whole `tasks.md`, sequential per-task loop, fresh context per task, one PR per spec / one commit per task, stop-at-first-blocker; optional parallelism). Supersedes an earlier interim "A — scoped" record. |
| [ADR-003](ADR-003-results-channel.md) | Results channel: polling vs streaming | ACCEPTED | **A — polling** for v1 |
| [ADR-004](ADR-004-idempotency-and-schema.md) | Idempotency key & DynamoDB schema | ACCEPTED | Generic `task_id`, additive schema |
| [ADR-005](ADR-005-payload-naming.md) | Payload contract naming / generalisation | ACCEPTED | Generalise to `TaskPayload`, keep field names |
| [ADR-006](ADR-006-result-delivery.md) | How the result reaches the dev: PR / branch / patch | ACCEPTED | **A — PR** |
| [ADR-007](ADR-007-concurrency-and-cost-guardrails.md) | Concurrency, quota & cost guardrails for scaled parallelism | ACCEPTED | **A+B+C+D (alert-only)+E** — reserved concurrency + worker cap via SQS backpressure + per-actor quota + budget alarms (alert-only, hard cap deferred) + audit log |
| [ADR-008](ADR-008-verification-gates.md) | Verification gates: adversarial review + verify-vs-spec before the PR | **ACCEPTED** | Bounded, B1-preserving gates via in-container `kiro-cli` (two passes), severity-gated draft routing, spec inputs added for Gate B (skip-with-notice if absent), default-off flags. **Q6 overridden → applies to BOTH the spec-executor (post-loop) AND the one-shot minion (post-fix).** Implementation in progress. |

See [../ide-ingress.md](../ide-ingress.md) for the ingress spec, and
[../verification-gates.md](../verification-gates.md) for the ADR-008 proposal
these decisions support.
