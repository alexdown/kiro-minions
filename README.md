# kiro-minions

Autonomous SonarQube remediation: a labelled Jira ticket goes in, a GitHub PR comes out.

```
Jira ticket (label=sonarqube + sonar-hash:<key>)
  → webhook → API Gateway → Orchestrator (Lambda, Python)
      → parse webhook body directly (no Jira API call)
      → extract structured header (clone URL, base branch, fix branch, file) + sonar_hash label
      → dedup on sonar_hash; send TicketPayload JSON to AgentCore Runtime
  → Runtime (custom container — deterministic pipeline, no outer model loop)
      → git clone → checkout base_branch → create fix_branch
      → kiro-cli headless (brief = ticket description) → run tests
      → retry loop (up to 3 attempts) → commit → push → open PR
  → GitHub PR (against base_branch)
```

## Architecture: B1 (deterministic pipeline)

The coding agent is a **deterministic Python script** running in an **AgentCore Runtime** custom container. There is no outer model loop — the only AI reasoning happens inside `kiro-cli` itself. The workflow steps (clone, branch, test, commit, push, PR) are plain sequential code.

**Why B1 over a managed harness?**

- **Single agent loop** — kiro-cli is the only model invocation. No tokens wasted on deciding to run `git clone`.
- **Predictable** — the workflow executes the same way every time. No model deviation.
- **Testable** — the pipeline is Python code you can unit-test and mock.
- **Fast** — deterministic steps run at shell speed; only the fix step waits for model inference.

## Flow

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
                                                                  │   payload=TicketPayload JSON)
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

## Layout

```
orchestrator/   Lambda: Jira webhook handler + AgentCore Runtime invoker
runtime/        AgentCore Runtime container (Dockerfile + deterministic pipeline script)
agent/          [LEGACY] Original managed-harness definition (kept for reference)
specs/          Design docs
deploy/         SAM template (API Gateway + Lambda + DynamoDB)
tests/          Unit tests
```

## Example ticket (PROJ-15)

```
Title: [SonarQube][P0] XSS — Swig template auto-escaping disabled (server.js:135)

Repo URL: https://github.com/your-org/your-repo
Clone: https://github.com/your-org/your-repo.git
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

## Deploy

### 1. Deploy the Runtime container

```bash
cd runtime

# Set your AWS account ID
export AWS_ACCOUNT_ID=123456789012

# Build and deploy via AgentCore starter toolkit
agentcore launch
```

This builds the ARM64 Docker image (with kiro-cli, gh, git pre-installed), pushes to ECR, and deploys an AgentCore Runtime. Note the **Runtime ARN** from the output.

### 2. Deploy the orchestrator stack

```bash
cd deploy

sam deploy \
  --stack-name kiro-minions \
  --parameter-overrides \
    AgentCoreRuntimeArn=arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/kiro-minions \
    JiraBaseUrl=https://your-org.atlassian.net \
  --capabilities CAPABILITY_IAM
```

### 3. Configure secrets

The Runtime container needs two env vars (set via AgentCore Runtime environment configuration or the execution role's access to SSM):

| Var | Purpose |
|-----|---------|
| `GITHUB_TOKEN` | Clone, push, open PR |
| `KIRO_API_KEY` | kiro-cli headless auth |

### 4. Wire the Jira webhook

Point your Jira webhook at the `WebhookUrl` output from the SAM stack.

## Credentials

| Var | Lives in | Used by |
|-----|----------|---------|
| `GITHUB_TOKEN` | Runtime container env | git clone/push, gh pr create |
| `KIRO_API_KEY` | Runtime container env | kiro-cli headless |
| `AGENTCORE_RUNTIME_ARN` | Orchestrator Lambda env | which Runtime to invoke |

The orchestrator needs **no external credentials** beyond AWS IAM (to call AgentCore `Invoke` and DynamoDB).

## Docs

- [specs/overview.md](specs/overview.md) — architecture and design rationale
- [specs/payload.md](specs/payload.md) — payload schema + ticket parsing
- [specs/runtime.md](specs/runtime.md) — Runtime container design and deployment
