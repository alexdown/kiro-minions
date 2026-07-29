# Overview

Autonomous SonarQube remediation: a labelled Jira ticket goes in, a GitHub PR comes out. No human in the loop between the two.

## The core idea: the coding agent is a deterministic pipeline, not a model loop

The coding agent is a **deterministic Python script** running in an **AgentCore Runtime custom container**. It is not a model-in-a-loop. The only AI reasoning happens inside `kiro-cli` — the workflow steps (clone, branch, test, commit, push, PR) are plain sequential code.

This is the **B1 architecture**: one model call (kiro-cli), deterministic everything else. See [runtime.md](runtime.md) for the full design rationale and tradeoffs.

## Flow

1. SonarQube automation (or an engineer) creates a Jira ticket for a finding. The ticket carries a `sonarqube` / `SONARQUBE-FIX` label and a `sonar-hash:<key>` label, and its **description contains everything the coding agent needs**.
2. Jira fires a webhook → API Gateway → **orchestrator** Lambda. The webhook body contains the full ticket: key, summary, description, labels.
3. The orchestrator parses the webhook body directly (no Jira API call needed), extracts the structured fields, builds a `TicketPayload`, checks the `sonar_hash` for idempotency, and invokes the AgentCore Runtime with the payload as JSON.
4. The **AgentCore Runtime** container receives the TicketPayload and runs the deterministic pipeline: clone the repo, create the fix branch, run `kiro-cli` headless, run tests (retrying up to 3 times), commit, push, and open a GitHub PR.

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

The orchestrator reads **Part 1** to populate the TicketPayload fields. The **entire description** (Part 1 + Part 2) is passed to the Runtime in the payload's `description` field — kiro-cli receives it as its brief.

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
                                                          │ build payload  │
                                                          └───────┬────────┘
                                                                  │ invoke(
                                                                  │   runtimeArn,
                                                                  │   runtimeSessionId,
                                                                  │   payload=JSON)
                                                                  ▼
                                            ┌──────────────────────────────────────────┐
                                            │       AgentCore Runtime (custom container) │
                                            │  (isolated microVM · deterministic script) │
                                            │                                            │
                                            │  agent.py — sequential pipeline:           │
                                            │    1. git clone (token-auth URL)           │
                                            │    2. git checkout -b fix_branch           │
                                            │    3. kiro-cli (headless, brief=desc)      │
                                            │    4. run tests (auto-detect runner)       │
                                            │    5. if fail → reset + retry (≤3x)       │
                                            │    6. git commit + push                    │
                                            │    7. gh pr create                         │
                                            └───────────────────┬────────────────────────┘
                                                                  │
                                                                  ▼
                                                               GitHub PR
```

The workflow steps are executed by **deterministic code**, not by a model reasoning in a loop. The only model invocation is kiro-cli (step 3).

## Components

### Orchestrator (`orchestrator/`)
Python Lambda behind API Gateway, triggered by the Jira webhook.

- Validates the incoming webhook body; ignores events without the `sonarqube` / `SONARQUBE-FIX` label.
- **Parses the webhook body directly** — `issue.key`, `issue.fields.summary`, `issue.fields.description`, and `issue.fields.labels` are all present. No Jira API call needed.
- From the description's **Part 1 header block**, extracts: `Clone`, `Base branch`, `File to fix`, `Suggested fix branch` (see [payload.md](payload.md)).
- From the **labels**, extracts the `sonar-hash:<key>` value → `sonar_hash`.
- **Idempotency:** before dispatching, atomically claims the `sonar_hash` (DynamoDB conditional put). If a job is already in-flight or done for this hash, it skips.
- **Invokes the Runtime** via boto3 `bedrock-agentcore` → `invoke`, passing `runtimeArn`, a fresh `runtimeSessionId`, and the TicketPayload as JSON bytes.

### Runtime (`runtime/`)
A custom Docker container (ARM64) deployed as an AgentCore Runtime:

- `runtime/Dockerfile` — pre-installs kiro-cli, gh, git, AWS CLI, Node.js.
- `runtime/agent.py` — the deterministic pipeline script (BedrockAgentCoreApp entrypoint).
- `runtime/requirements.txt` — Python dependencies (strands-agents, bedrock-agentcore).
- `runtime/.bedrock_agentcore.yaml` — deployment configuration for `agentcore launch`.

The pipeline receives the TicketPayload and runs:
1. Clone repo (GITHUB_TOKEN in URL for auth)
2. Create fix branch
3. Run kiro-cli headless with the issue description as the brief
4. Auto-detect and run the test suite
5. If tests fail, reset and retry (up to `MAX_FIX_ATTEMPTS`, default 3)
6. Commit with message `fix: {summary} [{ticket_id}]`
7. Push the fix branch
8. Open a PR via `gh pr create`

### Infrastructure (`deploy/`)
AWS SAM template providing:
- API Gateway HTTP API (webhook endpoint)
- Lambda function (orchestrator)
- DynamoDB table (idempotency on sonar_hash)
- IAM policies (InvokeRuntime on AgentCore, DynamoDB CRUD)

## Credentials (env vars)

| Var | Lives in | Used by | Purpose |
|-----|----------|---------|---------|
| `GITHUB_TOKEN` | Runtime | git, gh | Clone, push, open PR |
| `KIRO_API_KEY` | Runtime | kiro-cli | Headless auth |
| `AGENTCORE_RUNTIME_ARN` | Orchestrator | handler.py | Which Runtime to invoke |

The orchestrator needs **no external credentials** beyond AWS IAM.

## Design decision: B1 (deterministic) over managed harness

See [runtime.md](runtime.md) for the full comparison. Summary:

- **Previous (managed harness):** two nested model loops — the harness model reasons about git/test/PR workflow, then calls kiro-cli which does its own reasoning. Double token cost, double latency, model can deviate from the script.
- **Current (B1 Runtime):** one model call (kiro-cli). Everything else is sequential Python. Predictable, testable, cheaper, faster.
- **Future (B2):** if needed, add model reasoning only for error recovery on novel failures. Keep the happy path deterministic.
