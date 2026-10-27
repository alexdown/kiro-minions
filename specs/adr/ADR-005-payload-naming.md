# ADR-005: Payload contract naming / generalisation

**Status:** PROPOSED (needs your decision)
**Context doc:** [../ide-ingress.md](../ide-ingress.md) §5

## Context

The downstream (SQS → consumer → runtime) consumes a `TicketPayload` whose field
names are Jira-flavoured (`ticket_id`, `ticket_url`, `sonar_hash`). The IDE
ingress must produce a payload the **unchanged** downstream accepts. We want one
contract both ingresses emit, without churning the runtime.

## Options

### Option A — Generalise to `TaskPayload`, keep existing field names (recommended)
Rename the *concept* to `TaskPayload`, add `source` (`"jira"` | `"kiro-ide"`) and
`actor`, make Jira-only fields (`sonar_hash`, `ticket_url`) optional, and keep all
field names the runtime already reads (`repo_clone_url`, `base_branch`,
`fix_branch`, `file_to_fix`, `description`, `summary`). Jira ingress fills the
Jira fields; IDE ingress leaves them empty and fills `actor`/`source`.

- **Pros:** runtime unchanged (it reads the same field names); both sources emit
  one shape; `source`/`actor` give audit + routing; backward compatible.
- **Cons:** some fields are source-specific and optional (minor doc burden);
  "Ticket"→"Task" rename touches docs/tests naming.

### Option B — Keep `TicketPayload` as-is, overload it for IDE
Reuse `TicketPayload` verbatim; IDE tasks stuff an instruction into `description`
and synthesise dummy Jira-ish fields.

- **Pros:** zero contract change.
- **Cons:** misleading field names for IDE tasks; dummy values pollute data; no
  clean `source`/`actor`; harder to evolve.

### Option C — Separate payload types per source
`TicketPayload` (Jira) and a distinct `IdeTaskPayload`, converging only at the
runtime.

- **Pros:** each source's shape is precise.
- **Cons:** the downstream must branch on type or convert; more code; defeats the
  "one contract downstream" goal.

## Decision

_To be recorded by the maintainer._ Recommendation: **Option A** — one
`TaskPayload` superset, additive fields (`source`, `actor`), Jira fields optional,
runtime field names unchanged. Treat the "Ticket→Task" naming as a
docs/tests-level rename, not a wire-breaking change.

**Questions that decide this:**
1. OK to introduce `source` + `actor` and make `sonar_hash`/`ticket_url` optional?
2. Any objection to the "Ticket → Task" terminology shift in specs/tests?

## Consequences

- With Option A the runtime and consumer need no change beyond tolerating
  optional fields they already treat as optional (`file_to_fix`, `ticket_url`).
- `source`/`actor` become the basis for metrics, audit, and per-actor quota
  (ADR-007).
