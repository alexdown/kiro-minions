# ADR-004: Idempotency key & DynamoDB schema

**Status:** PROPOSED (needs your decision)
**Context doc:** [../ide-ingress.md](../ide-ingress.md) §5

## Context

Idempotency and the job lease are keyed today on `sonar_hash` (the DynamoDB
partition key; `_claim_job` does a conditional put with a lease). That key is
Jira/SonarQube-specific. IDE tasks have no `sonar_hash`; they need a generic
idempotency key so a dropped response or a double-click doesn't dispatch twice,
and so the same lease/reclaim semantics apply.

The existing table, lease logic, TTL, and status write-back are proven and
should be reused — the only question is how to key them generically without
breaking the Jira path.

## Options

### Option A — Generic `task_id`, additive schema on the same table (recommended)
Introduce a `task_id` as the canonical idempotency key. For Jira, `task_id` is
derived from / equals `sonar_hash` (stable mapping) so existing rows and behaviour
are unchanged; for IDE, the extension (or ingress) supplies/derives a stable
`task_id` (e.g. hash of actor+repo+base+instruction, or a client-supplied UUID).
Keep the same table and lease logic; the partition key is generalised in naming
only.

- **Pros:** one table, one lease implementation, one status model; Jira path
  unaffected; minimal change to `_claim_job`.
- **Cons:** needs a careful, documented mapping so Jira `sonar_hash` ↔ `task_id`
  stays stable; partition-key *rename* (if we rename the attribute) is a
  migration — safer to keep the physical attribute name and treat `task_id` as a
  logical alias.

### Option B — Separate table for IDE tasks
A new `tasks` table parallel to the `jobs` table.

- **Pros:** clean separation; no risk to the Jira table; independent TTL/capacity.
- **Cons:** two tables, two code paths for claim/status; the consumer/runtime must
  know which table to write back to (couples the generic downstream to the
  source); more to operate.

### Option C — Client-supplied idempotency token only
Trust the IDE to send a unique idempotency key per task; no server-side derivation.

- **Pros:** simplest server logic.
- **Cons:** correctness depends on the client; a buggy extension can double-dispatch
  or collide; weaker guarantee.

## Decision

_To be recorded by the maintainer._ Recommendation: **Option A** — generalise to
a logical `task_id` on the **same** table, keep the physical partition-key
attribute name to avoid a migration, map Jira `sonar_hash → task_id` 1:1, and let
the IDE ingress derive a stable `task_id` (with a client-supplied idempotency key
as an optional override).

**Questions that decide this:**
1. Keep one table (simpler downstream) or isolate IDE tasks in their own table?
2. Should the idempotency key be server-derived (collision-safe) or client-
   supplied (simpler)? Recommendation: server-derived with optional client key.
3. Are there retention/PII differences between Jira and IDE tasks that would
   argue for separate tables?

## Consequences

- Option A keeps the consumer and runtime source-agnostic (they already just read
  the payload and write status by key). The runtime's `write_job_status` stays
  unchanged if the key attribute name is unchanged.
- The `source` field (ADR-005) distinguishes rows for audit/metrics without
  needing separate tables.
