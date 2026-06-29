# kiro-minions 🤖

Skeletal demo of autonomous software engineering: a labelled SonarQube/Jira ticket goes in, a GitHub PR comes out.

```
Jira ticket (label=sonarqube + sonar-hash:<key>)
  → webhook → API Gateway → Orchestrator (Lambda, Python)
      → parse webhook body directly (no Jira API call)
      → extract structured header (clone URL, base branch, fix branch, file) + sonar_hash label
      → dedup on sonar_hash; render task prompt; invoke_harness(harnessArn, sessionId, messages)
  → Coding Agent (AgentCore managed harness — model in a loop, built-in shell tool)
      → git clone → checkout base_branch → create fix_branch
      → kiro-cli headless (brief = ticket description) → run tests (iterate on failure) → open PR
  → GitHub PR (against base_branch)
```

The coding agent is **not Python code** — it is an AgentCore *managed harness*: a stateful model-in-a-loop runtime with built-in `shell` and `file_operations` tools. The orchestrator calls `invoke_harness` with a task prompt; the harness runs git, kiro-cli, the tests, and `gh pr create` itself, iterating until the PR is open. We hardcode no steps and no retry logic.

The ticket description **is** the brief. It's two parts: a machine-readable header block (repo, branch, fix branch, file) followed by the full SonarQube issue report (description, impact, recommended fix). The orchestrator reads the header to drive mechanics; the agent gets the whole description verbatim as its kiro prompt.

## Example ticket (SW-15)

```
Title: [SonarQube][P0] XSS — Swig template auto-escaping disabled (server.js:135)

Repo URL: https://github.com/alexdown/NodeGoat
Clone: https://github.com/alexdown/NodeGoat.git
Base branch: master
File to fix: server.js:135
Suggested fix branch: sonarqube-fix/xss-swig-autoescape

SONAR-MCP-001: XSS — Template Auto-Escaping Disabled
Priority: P0 · CWE-79 · OWASP A03:2021 · Rule javascript:S5247
Recommended Fix:  swig.setDefaults({ autoescape: true });
_sonar-hash: 6f50f92a-65f2-4c4c-aae5-9b069b44430a_

Labels: CWE-79, OWASP-A03, P0, SONARQUBE-FIX, XSS, security,
        sonar-hash:6f50f92a-65f2-4c4c-aae5-9b069b44430a, sonarqube
```

The orchestrator owns Jira (read-only, from the webhook). The agent (harness) owns GitHub + kiro. The only thing crossing the boundary is the **task prompt** the orchestrator renders from the `TicketPayload`. The `sonar-hash` label is the idempotency key — one finding, one PR.

## Layout

```
orchestrator/   Lambda: Jira webhook handler + AgentCore harness invoker (invoke_harness)
agent/          AgentCore managed-harness definition (harness.json + system-prompt.md) — no app code
specs/          Design docs
```

## Docs

- [specs/overview.md](specs/overview.md) — architecture, components, data flow
- [specs/payload.md](specs/payload.md) — payload schema + ticket parsing + task prompt construction
- [specs/harness.md](specs/harness.md) — how to create/configure/deploy the AgentCore harness
