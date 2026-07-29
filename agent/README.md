# [LEGACY] Coding Agent — AgentCore Managed Harness

> **This directory is superseded by `runtime/`.** The project now uses a B1
> deterministic pipeline (AgentCore Runtime + custom container) instead of a
> managed harness. These files are kept for reference only.

See [../specs/runtime.md](../specs/runtime.md) for the current architecture.

---

## What this was

The original design used an AgentCore *managed harness* — a stateful model-in-a-loop (Strands Agents) with built-in `shell` and `file_operations` tools. The orchestrator rendered a natural-language task prompt and sent it to the harness via `invoke_harness`. The harness model (Claude) then reasoned about each workflow step (clone, branch, kiro-cli, test, push, PR).

## Why it was replaced

Two nested model loops:
1. **Outer:** harness model deciding to run `git clone`, `kiro-cli`, `npm test` (deterministic decisions)
2. **Inner:** kiro-cli reasoning about the actual code fix

The outer loop added token cost, latency, and model-deviation risk without adding value for a deterministic workflow. See [../specs/runtime.md](../specs/runtime.md) for the full comparison.
