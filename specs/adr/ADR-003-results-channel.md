# ADR-003: Results / feedback channel to the IDE

**Status:** PROPOSED (needs your decision)
**Context doc:** [../ide-ingress.md](../ide-ingress.md) §6

## Context

The Jira ingress is fire-and-forget: the PR appears later, out of band. A
developer who dispatched a task from the IDE expects feedback — at minimum the
final outcome, ideally coarse progress ("cloning → testing → PR opened"). We
already write a per-job status row to DynamoDB (`in_flight` / `done` + `pr_url` /
`failed` / `tests_failed`), so the backbone exists; the question is how the IDE
observes it.

## Options

### Option A — Polling (recommended v1)
Expose `GET /tasks/{task_id}` returning the current status row (status + redacted
reason + `pr_url` when done). The extension polls every few seconds until a
terminal status.

- **Pros:** simplest; stateless; cheap; reuses the status row we already write;
  no connection/fan-out state; trivial to test with the existing fakes.
- **Cons:** not truly real-time; polling overhead; coarse progress only unless we
  add intermediate `phase` updates.

### Option B — Streaming (WebSocket or SSE via API Gateway)
Push status transitions to the IDE as they happen.

- **Pros:** best UX; near-real-time milestones; no polling.
- **Cons:** connection lifecycle + auth-on-socket + fan-out to the right client;
  more infra and more to secure/test; harder to reason about at scale.

### Option C — Callback webhook to the IDE/customer
The worker POSTs the result to a customer-provided URL.

- **Pros:** no polling; decoupled.
- **Cons:** the IDE is on a laptop (often no reachable inbound URL); needs a
  customer-side receiver; mutual auth; poor fit for per-dev laptops.

## Decision

_To be recorded by the maintainer._ Recommendation: **Option A (polling) for
v1**, adding a coarse `phase` field to the status row (`queued` / `cloning` /
`fixing` / `testing` / `opening_pr` / terminal) so the IDE can show progress.
Revisit **Option B (streaming)** once the hand-off is proven and if UX demands it.

**Questions that decide this:**
1. Is "poll until done, then show the PR link" acceptable UX for v1, or is
   live progress a hard requirement?
2. If live progress matters, is a coarse `phase` (polled) enough, or do they
   want token-level streaming of the agent transcript (bigger scope)?

## Consequences

- Option A → one new read-only Lambda + route; the runtime optionally emits a
  couple of intermediate `phase` writes (small additive change).
- Option B → WebSocket API, connection store, and socket auth — a meaningfully
  larger build. Defer unless required.
