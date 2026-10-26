# ADR-007: Concurrency, quota & cost guardrails for scaled parallelism

**Status:** PROPOSED (needs your decision)
**Context doc:** [../ide-ingress.md](../ide-ingress.md) §8

## Context

The entire point of the IDE ingress is to lift the laptop as the parallelism
bottleneck — i.e. run **many** tasks concurrently in the cloud. That directly
raises exposure that the single-Jira-webhook flow never stressed:

- AWS Lambda concurrency (ingress, consumer),
- AgentCore Runtime session limits (how many workers can run at once),
- Bedrock token spend per task (kiro-cli inference),
- a single developer (or a buggy extension) dispatching a flood of tasks.

Backlog items I3 (concurrency limits) and I5 (cost guardrails) were "Medium /
nice to have" for the Jira flow. For this feature they become **required**:
without them, scaled parallelism is also scaled cost and scaled blast radius.

## Options

These are not mutually exclusive — the decision is *which to include in v1*.

### Control A — Lambda reserved/max concurrency
Cap the ingress and consumer Lambda concurrency so a burst can't fan out without
bound.
- **Pros:** simple, native, protects downstream; **Cons:** too low throttles
  legitimate load — needs a sensible number.

### Control B — AgentCore session cap / backpressure
Bound how many Runtime workers run at once; SQS naturally buffers the rest (the
queue is the backpressure mechanism).
- **Pros:** reuses SQS as a buffer; protects Bedrock spend; **Cons:** need to pick
  a worker ceiling aligned to account/service limits.

### Control C — Per-actor dispatch quota / rate limit
Limit tasks per developer per window (using `actor` from ADR-001).
- **Pros:** stops one dev/extension monopolising or flooding; fairness;
  **Cons:** needs identity (ADR-001) and a counter store.

### Control D — Cost alarms & budget cap
AWS Budgets alerts on Bedrock/AgentCore spend; optionally a hard daily dispatch
cap that fails-closed when exceeded.
- **Pros:** catches runaway spend; **Cons:** a hard cap can block legitimate work —
  decide alert-only vs enforce.

### Control E — Audit log of dispatch decisions
Immutable record: who dispatched what, when, claim outcome, terminal status
(backlog O7). Not a throttle, but essential for multi-tenant operation and for
debugging cost/abuse.
- **Pros:** accountability; **Cons:** a little extra write volume.

## Decision

_To be recorded by the maintainer._ Recommendation for **v1 minimum**:
**A (Lambda concurrency) + B (worker cap, SQS backpressure) + C (per-actor
quota) + D (budget alarms, alert-only to start) + E (audit log)**. Defer a hard
enforced spend cap until you've seen real usage.

**Questions that decide this:**
1. What are the real concurrency ceilings (AWS account limits, AgentCore session
   limits, acceptable Bedrock spend/day)?
2. Per-actor quota: tasks/hour/dev? Alert-only or enforce?
3. Budget guardrail: alert-only, or hard fail-closed cap? At what threshold?
4. Is an audit log a v1 requirement (recommended for multi-tenant) or a follow-up?

## Consequences

- C depends on ADR-001 (identity) landing first.
- These controls shape the SAM template (reserved concurrency, alarms), the
  ingress Lambda (quota check), and DynamoDB/log usage (audit + counters).
- Deferring them is a real risk: scaled parallelism without guardrails is the
  one failure mode most likely to cause a surprise bill or a noisy-neighbour
  outage.
