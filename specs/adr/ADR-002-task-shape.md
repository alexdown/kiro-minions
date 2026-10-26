# ADR-002: Task shape for v1 — scoped vs open-ended

**Status:** PROPOSED (needs your decision — this is the pivotal one)
**Context doc:** [../ide-ingress.md](../ide-ingress.md) §4

## Context

The existing AgentCore Runtime (`runtime/agent.py`) is a **deterministic B1
pipeline**: one `kiro-cli` brief, auto-detected tests, up to `MAX_FIX_ATTEMPTS`
retries, one PR. It is built for a **bounded, verifiable task** ("fix this
finding; success = tests pass"). This is what makes it cheap, predictable, and
testable, and it is the design the project deliberately chose over a managed
model loop (see [../runtime.md](../runtime.md)).

"Work assigned from an IDE" can mean very different things, and the answer
determines whether we reuse this runtime or build a richer one.

## Options

### Option A — Scoped tasks (recommended v1)
The IDE sends a bounded task: repo + base branch + a target (file/area) + a
natural-language instruction + acceptance = *tests pass*. Maps ~1:1 onto today's
pipeline (`description` → kiro-cli brief).

- **Pros:** reuses the runtime unchanged; keeps B1 (no outer model loop);
  verifiable acceptance (tests); lowest cost/latency; fastest to ship; proves the
  hand-off and the scaling story with minimal new surface.
- **Cons:** constrains what a dev can assign; "implement a feature" doesn't fit;
  acceptance limited to the auto-detected test suite.

### Option B — Open-ended tasks
"Implement feature X across the repo." Likely needs a plan→code→review loop (a
stateful, model-in-a-loop worker), not the single-shot deterministic pipeline.

- **Pros:** much more capable; covers the general "assign any work" ask.
- **Cons:** reopens the outer-model-loop question B1 avoided; new and less
  predictable failure modes; higher and more variable cost per task; harder to
  define "done" (no simple acceptance gate); significantly larger build and test
  surface; multi-tenant cost risk is larger.

### Option C — Both, via two runtime variants
Keep the deterministic runtime for scoped tasks; add a *separate* richer worker
for open-ended ones, selected by a `task_type` field.

- **Pros:** doesn't compromise the deterministic path; grows capability without
  regressing the proven core.
- **Cons:** two runtimes to build/operate; only worth it once Option A is proven.

## Decision

_To be recorded by the maintainer._ Recommendation: **Option A for v1**, with
**Option C as the growth path** (add an open-ended worker as a second variant
later, rather than mutating the deterministic one). This keeps the first
increment small and preserves the B1 guarantees.

**Questions that decide this:**
1. What tasks does the customer actually want to hand off first — scoped fixes
   (lint/type/test failures, SonarQube-style findings, "fix this file") or
   open-ended feature work?
2. Is "success = the repo's tests pass" an acceptable acceptance gate for v1?
3. If open-ended is required soon, is a second worker variant (Option C)
   acceptable, or must it be one unified worker?

## Consequences

- Option A → the runtime needs little/no change; the work is the ingress +
  results channel. Option B → a new worker and a new set of reliability/cost
  controls. This choice sizes the whole feature.
- Feeds ADR-005 (payload fields — a `task_type` only matters under B/C) and
  ADR-007 (open-ended tasks need tighter cost guardrails).
