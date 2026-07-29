# [LEGACY] Harness

> **This spec is superseded by [runtime.md](runtime.md).** The project now uses
> a B1 deterministic pipeline (AgentCore Runtime + custom container) instead of
> a managed harness.

The original managed-harness design is documented below for historical reference.
The `agent/` directory still contains the harness configuration files (`harness.json`,
`system-prompt.md`) but they are no longer deployed.

---

## Original design (archived)

The coding agent was an **AWS Bedrock AgentCore managed harness** — a stateful agent runtime that ran a model in a ReAct loop with built-in tools. The orchestrator rendered a natural-language task prompt and sent it via `invoke_harness`. The harness model (Claude) autonomously drove git, kiro-cli, tests, and PR creation using its built-in `shell` tool.

### Why it was replaced

- **Double token cost:** the outer model burned tokens on deterministic workflow decisions (clone, branch, commit, push).
- **Double latency:** two model inference calls per step.
- **Model deviation risk:** the outer model could go off-script (wrong branch name, forgotten push).
- **Opacity:** the outer model couldn't see kiro-cli's internal reasoning.

The B1 architecture (see [runtime.md](runtime.md)) collapses this to a single model call (kiro-cli) with deterministic code handling everything else.
