# kiro-minions 🤖

Skeletal demo of autonomous software engineering: a labelled Jira ticket goes in, a GitHub PR comes out.

```
Jira ticket (label=SONARQUBE_FIX)
  → webhook → API Gateway → Orchestrator (Lambda, Python)
      → read ticket via Jira REST, transition to "In Progress"
      → invoke_agent(payload)
  → Coding Agent (AgentCore managed harness)
      → git clone → kiro-cli headless → run tests → open PR
  → GitHub PR
```

The orchestrator owns Jira. The agent owns GitHub + kiro. Neither reaches into the other's domain — the only thing crossing the boundary is a JSON `TicketPayload`.

## Layout

```
orchestrator/   Lambda: Jira webhook handler + AgentCore invoker
agent/          AgentCore managed-harness coding agent (runs kiro-cli)
specs/          Design docs
```

## Docs

- [specs/overview.md](specs/overview.md) — architecture, components, data flow
- [specs/payload.md](specs/payload.md) — payload schema + ticket parsing convention
