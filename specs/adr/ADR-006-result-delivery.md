# ADR-006: How the result reaches the developer

**Status:** PROPOSED (needs your decision)
**Context doc:** [../ide-ingress.md](../ide-ingress.md) §7

## Context

The developer's laptop holds local, possibly-uncommitted state; the AgentCore
worker operates on a **clean clone** from the remote. We must define, explicitly,
how the worker's output gets back to the dev and what state the worker acts on.
The customer's extension already "enforces a dev workflow", so this choice must
fit that workflow.

A shared prerequisite for all options: the worker acts on **pushed/committed**
state. The extension must ensure the base branch (and any WIP it wants acted on)
is pushed before dispatch — the worker cannot see unpushed local changes.

## Options

### Option A — Pull request (like today, recommended)
Worker pushes a fix branch and opens a PR; the IDE surfaces the PR link (via the
status row's `pr_url`).

- **Pros:** reuses the entire existing push+PR mechanism (zero new surface); fits
  an "enforced dev workflow" with review gates; natural audit trail; the dev
  reviews before anything lands.
- **Cons:** heavier than "just give me the diff"; requires repo PR permissions.

### Option B — Branch only (no PR)
Worker pushes a fix branch; the dev pulls/checks it out locally.

- **Pros:** lighter; dev stays in their local flow; no PR noise.
- **Cons:** skips review/CI gates the customer may rely on; dev must manually
  fetch; less auditable.

### Option C — Patch artifact
Worker produces a diff/patch the IDE downloads and applies locally.

- **Pros:** most "local-feeling"; no remote branch created; dev applies on top of
  their working tree.
- **Cons:** loses clean-clone isolation benefits; needs a new artifact channel
  (store + fetch); merge conflicts against local WIP; weaker provenance.

## Decision

_To be recorded by the maintainer._ Recommendation: **Option A (PR)** for v1 — it
reuses what exists and matches an enforced-workflow culture. Offer **Option B
(branch)** as a per-task toggle later if devs want a lighter loop. Treat **Option
C (patch)** as out of scope for v1.

**Questions that decide this:**
1. Does the enforced dev workflow already centre on PRs (then A is the obvious
   fit), or do devs expect changes applied straight into their working tree?
2. Is it acceptable that the worker only acts on *pushed* state (dev must push
   base/WIP first)?
3. Should PRs be authored as a bot (with the dev noted) or attributed to the dev?
   (ties into ADR-001).

## Consequences

- Option A → no new delivery mechanism; the `pr_url` already flows through the
  status row to the IDE (ADR-003).
- Options B/C → new behaviour in the runtime's final step and (for C) a new
  artifact store + fetch API.
