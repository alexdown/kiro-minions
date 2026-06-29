# kiro-minions 🤖

Skeletal demo: Jira ticket in → autonomous code fix → GitHub PR out.

```
Orchestrator (Python script)
  → polls Jira for SONARQUBE_FIX tickets
  → invokes one AgentCore agent per ticket (passes full context)

Coding Agent (AgentCore + kiro-cli headless)
  → receives ticket context
  → git clone → branch → kiro fix → test → PR
```

## Structure

```
orchestrator/   - Jira poller + AgentCore invoker
agent/          - Coding agent container (kiro-cli inside)
specs/          - Design docs
```

## Docs

- [specs/overview.md](specs/overview.md)
- [specs/payload.md](specs/payload.md)
