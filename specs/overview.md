# Overview

Autonomous SonarQube remediation: a labelled Jira ticket goes in, a GitHub PR comes out. No human in the loop between the two.

## The core idea: the coding agent is a harness, not a function

The coding agent is **not** a Python application that we write, package, and run. It is an **AWS Bedrock AgentCore managed harness** — a stateful agent runtime.

A harness:

- Runs a **model in a ReAct loop**: think → call a tool → observe the result → think again → repeat, until the model decides the job is done.
- Ships with **built-in tools** in every session — `shell` and `file_operations` — so it can run `git`, `kiro-cli`, `npm test`, `gh pr create`, edit files, etc. natively. **We write no custom tools** for git, kiro, or testing.
- Gives each session an **isolated microVM** with its own filesystem and shell.
- Runs **long-lived sessions** — there is no 600s Lambda timeout to design around.

So our "coding agent" is just two things:

1. A **harness configuration** (model, system prompt, env vars, allowed tools) — see [harness.md](harness.md).
2. An **initial task prompt** the orchestrator constructs per ticket and sends at invocation time.

The harness provides the environment; **the model figures out the steps**. We do not hardcode `clone → branch → kiro → test → push → PR` in code. We describe the goal in the prompt and let the agent drive the loop, iterating on its own when kiro produces a bad fix or tests fail.

## Flow

1. SonarQube automation (or an engineer) creates a Jira ticket for a finding. The ticket carries a `sonarqube` / `SONARQUBE-FIX` label and a `sonar-hash:<key>` label, and its **description contains everything the coding agent needs** (see below).
2. Jira fires a webhook → API Gateway → **orchestrator** Lambda. The webhook body contains the full ticket: key, summary, description, labels.
3. The orchestrator parses the webhook body directly (no Jira API call needed), extracts the structured fields, builds a `TicketPayload`, checks the `sonar_hash` for idempotency, **constructs a task prompt** from the payload, and invokes the harness via `invoke_harness`.
4. The **AgentCore harness** spins up an isolated session, receives the task prompt, and runs its ReAct loop using the built-in `shell` tool: clone the repo, create the fix branch, run `kiro-cli` headless, run tests (iterating if they fail), commit, push, and open a GitHub PR. The agent decides when it's done — when the PR is open.

## The ticket description is the contract

The Jira description is authored as a self-contained task brief in **two parts**:

```
── Part 1: structured header block (machine-readable) ──
Repo URL: https://github.com/your-org/your-repo
Clone:    https://github.com/your-org/your-repo.git
Base branch: master
File to fix: server.js:135
Suggested fix branch: sonarqube-fix/xss-swig-autoescape

── Part 2: inline SonarQube issue report (human + agent readable) ──
SONAR-MCP-001: XSS — Template Auto-Escaping Disabled
Priority: P0 (Critical) · Type: Security Vulnerability · CWE-79 · OWASP A03:2021
File: your-repo/server.js · Line: 135 · Rule: javascript:S5247
Hotspot Key: 6f50f92a-65f2-4c4c-aae5-9b069b44430a

Description / Impact / Recommended Fix:
  swig.setDefaults({ autoescape: true });

References · _sonar-hash: 6f50f92a-... (dedup marker)_
```

The orchestrator reads **Part 1** to drive the mechanics (which repo, which branch, which file) and embeds them in the task prompt. The **entire description** (including Part 2) is embedded **verbatim** in the task prompt as the SonarQube issue brief — Part 2 already reads like a brief: what's wrong, why it matters, and the exact recommended fix.

## Architecture

```
            webhook (issue created/updated, label=sonarqube + sonar-hash:<key>)
  Jira  ─────────────────────────────────────────────────►  API Gateway
                                                                  │
                                                                  ▼
                                                          ┌────────────────┐
                                                          │  Orchestrator  │  (Lambda, Python)
                                                          │                │
                                                          │ parse webhook  │
                                                          │ body → fields  │
                                                          │ dedup on hash  │
                                                          │ build prompt   │
                                                          └───────┬────────┘
                                                                  │ invoke_harness(
                                                                  │   harnessArn,
                                                                  │   runtimeSessionId,
                                                                  │   messages=[task_prompt])
                                                                  ▼
                                            ┌──────────────────────────────────────────┐
                                            │      AgentCore Managed Harness            │
                                            │  (isolated microVM · stateful · long-lived)│
                                            │                                            │
                                            │  model (Claude Sonnet/Opus on Bedrock)     │
                                            │  ReAct loop ── built-in tools:             │
                                            │      • shell                               │
                                            │      • file_operations                     │
                                            │                                            │
                                            │  think → shell(...) → observe → think → …  │
                                            └───────────────────┬────────────────────────┘
                                                                  │ (agent drives, in a loop)
   git clone (repo_clone_url) ──► checkout base_branch ──► create fix_branch
        ──► kiro-cli chat --no-interactive --trust-all-tools (brief = description)
        ──► run tests ──► (iterate with kiro if they fail) ──► commit
        ──► push fix_branch ──► open PR against base_branch (gh CLI / API)
                                                                  │
                                                                  ▼
                                                               GitHub PR
```

Everything below the harness box is run **by the model**, via the `shell` tool, in a loop — not by code we wrote.

## Components

### Orchestrator (`orchestrator/`)
Python Lambda behind API Gateway, triggered by the Jira webhook.

- Validates the incoming webhook body; ignores events without the `sonarqube` / `SONARQUBE-FIX` label.
- **Parses the webhook body directly** — `issue.key`, `issue.fields.summary`, `issue.fields.description`, and `issue.fields.labels` are all present. No Jira API call needed.
- From the description's **Part 1 header block**, extracts: `Repo URL` / `Clone`, `Base branch`, `File to fix`, `Suggested fix branch` (see [payload.md](payload.md)).
- From the **labels**, extracts the `sonar-hash:<key>` value → `sonar_hash`.
- **Idempotency:** before dispatching, atomically claims the `sonar_hash` (DynamoDB conditional put). If a job is already in-flight or done for this hash, it skips — no duplicate PRs for the same finding.
- **Builds the task prompt** from the `TicketPayload` (see [payload.md](payload.md) → "Task prompt construction").
- Invokes the harness via boto3 `bedrock-agentcore` → `invoke_harness`, passing `harnessArn`, a fresh `runtimeSessionId` (UUID, ≥33 chars), and a single user message containing the task prompt. The response is a stream; the orchestrator aggregates it for logging.

### Coding Agent (`agent/`)
**Not a Python application.** The `agent/` directory contains the harness *definition*, not runnable code:

- `agent/harness.json` — the AgentCore harness configuration (agentcore CLI project format): model, allowed tools, env vars.
- `agent/system-prompt.md` — the agent's persona/instructions, loaded as the harness system prompt.
- `agent/README.md` — how to deploy the harness and obtain its ARN.

At runtime the harness is a managed AgentCore runtime. It receives the task prompt and runs autonomously:

1. Uses the built-in `shell` tool to `git clone <repo_clone_url>` (auth via the `GITHUB_TOKEN` env var: `https://x-token:$GITHUB_TOKEN@github.com/...`), check out `base_branch`, and create `fix_branch`.
2. Runs kiro headless with the SonarQube issue as the brief:
   ```bash
   kiro-cli chat --no-interactive --trust-all-tools  # brief piped via stdin
   ```
   `KIRO_API_KEY` is the only kiro auth, injected as an env var. A repo may ship a custom persona at `.kiro/agents/<name>.json`; kiro-cli picks it up automatically.
3. Auto-detects and runs the repo's test suite (`npm test` / `mvn test` / `pytest` / `make test`).
4. **If tests fail, the agent iterates** — it reads the failure, re-runs kiro-cli with the failure context, and retries. This loop is the model's job, not hardcoded retry logic in our code.
5. Commits (`fix: {summary} [{ticket_id}]`), pushes `fix_branch`, and opens a PR **against `base_branch`** via the GitHub CLI or API. The PR body backlinks `ticket_url` and notes the `sonar_hash`.

The agent has **no Jira credentials** — its blast radius is GitHub + kiro only. Closing the loop back to Jira (if wanted) is the orchestrator's job.

## Harness configuration (summary)

Set up once; the orchestrator only needs the resulting ARN. Full detail in [harness.md](harness.md).

| Setting | Value |
|---|---|
| Model | Claude Sonnet (or Opus) on Bedrock |
| Built-in tools | `shell`, `file_operations` |
| `allowedTools` | `["shell", "file_*"]` |
| System prompt | `agent/system-prompt.md` |
| Env vars | `GITHUB_TOKEN`, `KIRO_API_KEY` (via AgentCore Identity Token Vault or injected at invoke time) |
| Custom container | none — the default harness environment already has shell access |

## Credentials (env vars)

| Var | Used by | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | harness | clone repo, push branch, open PR |
| `KIRO_API_KEY` | harness | kiro-cli headless auth |
| `AGENTCORE_HARNESS_ARN` | orchestrator | which harness to `invoke_harness` |

The orchestrator needs **no external credentials** beyond AWS IAM (to call `invoke_harness` and read/write its own DynamoDB idempotency state). Everything it needs about the ticket arrives in the webhook body. `GITHUB_TOKEN` and `KIRO_API_KEY` live with the **harness**, not the orchestrator.
