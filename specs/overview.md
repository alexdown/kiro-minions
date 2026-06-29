# Overview

## What it does

1. **Jira webhook** triggers a Lambda when a ticket with label `SONARQUBE_FIX` is created or updated
2. Lambda packages the ticket content into a JSON payload and invokes an AgentCore coding agent (managed harness)
3. **Coding agent** receives the payload, clones the repo, runs kiro-cli headless, tests, opens a PR

## Architecture

```
Jira (SONARQUBE_FIX ticket created/updated)
  → webhook → API Gateway → Lambda (orchestrator)
    → package payload
    → invoke AgentCore managed harness agent
      → git clone → branch → kiro headless → test → PR
```

## Components

### Orchestrator (`orchestrator/`)
- AWS Lambda triggered by Jira webhook (via API Gateway)
- Uses official Atlassian MCP to read ticket details
- Builds `TicketPayload` and invokes the AgentCore agent
- Transitions Jira ticket to "In Progress" to avoid re-processing

### Coding Agent (`agent/`)
- AgentCore **managed harness** (no custom container to maintain)
- Receives `TicketPayload` as input — never calls Jira
- Flow: `git clone` → `git checkout -b fix/TICKET-ID` → `kiro --headless <task>` → run tests → open PR
- Returns `{ status, pr_url, error }`

## Jira ticket format (expected)
```
Summary: <short description>
Description:
  REPO: https://github.com/org/repo
  BRANCH: main
  ---
  <what needs to be fixed and why>
```
The `REPO:` and `BRANCH:` lines are parsed by the orchestrator to populate the payload.

## Credentials
- `GITHUB_TOKEN` — clone + PR (coding agent)
- `JIRA_TOKEN` — Atlassian MCP auth (orchestrator)
- `KIRO_TOKEN` — kiro-cli auth (coding agent, if required)

## Open questions
1. **kiro headless syntax** — docs at kiro.dev/docs/cli/headless/ are JS-rendered, couldn't scrape. Need: exact command, how task/prompt is passed (flag? stdin? file?). TODO: check manually.
2. **AgentCore managed harness** — confirm invocation API: is it `bedrock-agent-runtime` `invoke_agent`, or a different endpoint for the managed harness?
