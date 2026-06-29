# Overview

Autonomous SonarQube remediation: a labelled Jira ticket goes in, a GitHub PR comes out. No human in the loop between the two.

## Flow

1. SonarQube automation (or an engineer) creates a Jira ticket for a finding. The ticket carries a `sonarqube` / `SONARQUBE-FIX` label and a `sonar-hash:<key>` label, and its **description contains everything the coding agent needs** (see below).
2. Jira fires a webhook → API Gateway → **orchestrator** Lambda. The webhook body contains the full ticket: key, summary, description, labels.
3. The orchestrator parses the webhook body directly (no Jira API call needed), extracts the structured fields, builds a `TicketPayload`, checks the `sonar_hash` for idempotency, and invokes the **coding agent**.
4. The coding agent receives the payload (and nothing else — it never touches Jira), clones the repo, checks out the suggested fix branch, runs `kiro-cli` headless with the full description as its task brief, runs the tests, and opens a GitHub PR against the base branch.

## The ticket description is the contract

The Jira description is authored as a self-contained task brief in **two parts**:

```
── Part 1: structured header block (machine-readable) ──
Repo URL: https://github.com/alexdown/NodeGoat
Clone:    https://github.com/alexdown/NodeGoat.git
Base branch: master
File to fix: server.js:135
Suggested fix branch: sonarqube-fix/xss-swig-autoescape

── Part 2: inline SonarQube issue report (human + agent readable) ──
SONAR-MCP-001: XSS — Template Auto-Escaping Disabled
Priority: P0 (Critical) · Type: Security Vulnerability · CWE-79 · OWASP A03:2021
File: NodeGoat/server.js · Line: 135 · Rule: javascript:S5247
Hotspot Key: 6f50f92a-65f2-4c4c-aae5-9b069b44430a

Description / Impact / Recommended Fix:
  swig.setDefaults({ autoescape: true });

References · _sonar-hash: 6f50f92a-... (dedup marker)_
```

The orchestrator reads **Part 1** to drive the mechanics (which repo, which branch, which file). The coding agent receives the **entire description verbatim** as its kiro prompt — Part 2 already reads like a task brief: what's wrong, why it matters, and the exact recommended fix.

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
                                                          └───────┬────────┘
                                                                  │ invoke_agent(TicketPayload)
                                                                  ▼
                                                          ┌────────────────┐
                                                          │  Coding Agent  │  (AgentCore managed harness)
                                                          └───────┬────────┘
                                                                  │
   git clone (repo_clone_url) ──► checkout base_branch ──► create fix_branch
        ──► kiro-cli headless (prompt = full description) ──► edit file_to_fix
        ──► run tests ──► push fix_branch ──► open PR against base_branch
                                                                  │
                                                                  ▼
                                                               GitHub PR
```

## Components

### Orchestrator (`orchestrator/`)
Python Lambda behind API Gateway, triggered by the Jira webhook.

- Validates the incoming webhook body; ignores events without the `sonarqube` / `SONARQUBE-FIX` label.
- **Parses the webhook body directly** — `issue.key`, `issue.fields.summary`, `issue.fields.description`, and `issue.fields.labels` are all present. No Jira API call needed.
- From the description's **Part 1 header block**, extracts: `Repo URL` / `Clone`, `Base branch`, `File to fix`, `Suggested fix branch` (see [payload.md](payload.md)).
- From the **labels**, extracts the `sonar-hash:<key>` value → `sonar_hash`.
- **Idempotency:** before dispatching, checks whether a PR or an in-flight job already exists for this `sonar_hash` (e.g. an open PR from `fix_branch`, or a job-state record / in-progress label). If found, it skips — no duplicate PRs for the same finding.
- Invokes the coding agent via boto3 `bedrock-agent-runtime` → `invoke_agent`, passing the `TicketPayload` as input. The full description is forwarded **verbatim**.

### Coding Agent (`agent/`)
An **AgentCore managed harness** — no custom container to build or maintain. AgentCore hands it the JSON payload as input.

Flow:
1. `git clone <repo_clone_url>` using `GITHUB_TOKEN`, check out `base_branch`, create `fix_branch` (the suggested branch from the ticket, e.g. `sonarqube-fix/xss-swig-autoescape`).
2. Run kiro headless with the **full ticket description** as the task prompt. The description already contains the repo context, the file/line to fix, the rule, and the exact recommended fix — it is the brief:
   ```bash
   kiro-cli chat --no-interactive --trust-all-tools "$DESCRIPTION"
   ```
   For large descriptions, pipe via stdin:
   ```bash
   cat description.txt | kiro-cli chat --no-interactive --trust-all-tools "Fix the SonarQube finding described in the attached task. Target file: $FILE_TO_FIX"
   ```
   `KIRO_API_KEY` is the only auth. A repo may ship a custom persona at `.kiro/agents/<name>.json`; if present, kiro-cli picks it up automatically.
3. The agent focuses edits on `file_to_fix` (e.g. `server.js:135`) but is free to touch what the fix requires.
4. Run the repo's test suite. On failure, the agent iterates or bails (returns `error`).
5. Push `fix_branch` and open a PR **against `base_branch`** with `GITHUB_TOKEN`. The PR body backlinks `ticket_url` and notes the `sonar_hash`.

Returns `{ status, pr_url, error }`. The agent has **no Jira credentials** — closing the loop back to Jira (if wanted) is the orchestrator's job, not the agent's.

## Credentials (env vars)

| Var | Used by | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | agent | clone repo, push branch, open PR |
| `KIRO_API_KEY` | agent | kiro-cli headless auth |

The orchestrator needs **no external credentials** beyond AWS IAM (to invoke AgentCore and read/write its own idempotency state). Everything it needs about the ticket arrives in the webhook body. The agent's blast radius is limited to GitHub + kiro only.
