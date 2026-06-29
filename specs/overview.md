# Overview

Autonomous SonarQube remediation: a labelled Jira ticket goes in, a GitHub PR comes out. No human in the loop between the two.

## Flow

1. An engineer (or SonarQube automation) creates/updates a Jira ticket labelled `SONARQUBE_FIX`.
2. Jira fires a webhook → API Gateway → **orchestrator** Lambda.
3. The orchestrator reads the ticket via the Jira REST API, builds a `TicketPayload`, transitions the ticket to **In Progress**, and invokes the **coding agent**.
4. The coding agent receives the payload (and nothing else — it never touches Jira), clones the repo, runs `kiro-cli` headless to produce the fix, runs the tests, and opens a GitHub PR.

## Architecture

```
                            webhook (ticket created/updated, label=SONARQUBE_FIX)
  Jira  ──────────────────────────────────────────────►  API Gateway
   ▲                                                          │
   │ REST: read fields, transition to "In Progress"          ▼
   │                                                  ┌───────────────┐
   └──────────────────────────────────────────────── │  Orchestrator │  (Lambda, Python)
                                                       └───────┬───────┘
                                                               │ invoke_agent(payload)
                                                               ▼
                                                       ┌───────────────┐
                                                       │  Coding Agent │  (AgentCore managed harness)
                                                       └───────┬───────┘
                                                               │
                  git clone ──► kiro-cli headless ──► run tests ──► open PR
                                                               │
                                                               ▼
                                                            GitHub PR
```

## Components

### Orchestrator (`orchestrator/`)
Python Lambda behind API Gateway, triggered by the Jira webhook.

- Validates the webhook payload; ignores tickets without the `SONARQUBE_FIX` label.
- Calls the **Jira REST API** directly (`JIRA_BASE_URL` + `JIRA_TOKEN`). No MCP — it's too heavy for a Lambda and buys nothing here.
- Parses `REPO:` / `BRANCH:` from the ticket description (see [payload.md](payload.md)).
- Transitions the ticket to **In Progress** *before* dispatch. This is the idempotency guard: re-fired webhooks for an in-progress ticket are dropped.
- Invokes the coding agent via boto3 `bedrock-agent-runtime` → `invoke_agent`, passing the `TicketPayload` as input.

### Coding Agent (`agent/`)
An **AgentCore managed harness** — no custom container to build or maintain. AgentCore hands it the JSON payload as input.

Flow:
1. `git clone` the repo using `GITHUB_TOKEN`, check out `default_branch`, create `fix/<ticket_id>`.
2. Run kiro headless with the ticket summary + description as the task prompt:
   ```bash
   kiro-cli chat --no-interactive --trust-all-tools "<prompt>"
   ```
   `KIRO_API_KEY` is the only auth. A repo may ship a custom persona at `.kiro/agents/<name>.json`; if present, kiro-cli picks it up automatically. Large prompts can be piped via stdin:
   ```bash
   cat task.txt | kiro-cli chat --no-interactive --trust-all-tools "Fix per the attached task"
   ```
3. Run the repo's test suite. On failure, the agent iterates or bails (returns `error`).
4. Push the branch and open a PR with `GITHUB_TOKEN`.

Returns `{ status, pr_url, error }`. The agent has **no Jira credentials** — closing the loop back to Jira (if wanted) is the orchestrator's job, not the agent's.

## Credentials (env vars)

| Var | Used by | Purpose |
|---|---|---|
| `JIRA_BASE_URL`, `JIRA_TOKEN` | orchestrator | read ticket, transition status |
| `GITHUB_TOKEN` | agent | clone repo, open PR |
| `KIRO_API_KEY` | agent | kiro-cli headless auth |

The agent's blast radius is deliberately limited to GitHub + kiro. It cannot read or mutate Jira.
