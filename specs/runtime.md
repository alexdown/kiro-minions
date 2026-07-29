# Runtime

The coding agent is a **deterministic Python script** running inside an **AgentCore Runtime custom container**. It replaces the previous managed-harness approach (two nested model loops) with a single-loop architecture where only `kiro-cli` performs AI reasoning.

## Why Runtime over Managed Harness?

The previous architecture used an AgentCore managed harness (Strands-powered model-in-a-loop) that drove `kiro-cli` via its built-in `shell` tool. This created **two nested agent loops**:

1. Outer loop: Claude reasoning about which shell commands to run (clone, branch, test, push…)
2. Inner loop: kiro-cli reasoning about the actual code fix

The outer loop added cost, latency, and a failure mode (model deviation) without adding value — because the workflow is deterministic. The B1 architecture collapses this to:

1. **Deterministic script** handles the mechanical workflow (clone, branch, test, commit, push, PR)
2. **kiro-cli** handles the only creative step (the code fix)

### Tradeoffs accepted

| Gained | Gave up |
|--------|---------|
| ~50% lower token cost (no outer model) | Model-driven error recovery on novel failures |
| Predictable, testable workflow | Adaptability to unexpected edge cases |
| Lower latency per task | "Self-healing" behavior on weird build systems |
| No prompt drift on workflow steps | Zero-code maintenance (now ~150 lines of Python) |

For a small-scale test this is the right tradeoff. If novel failure patterns emerge at scale, we can add targeted model reasoning for those cases (evolving toward B2).

## Container contents

The Docker image (`runtime/Dockerfile`) is ARM64 and pre-installs:

| Tool | Purpose |
|------|---------|
| `kiro-cli` | Headless code fix (the only AI call) |
| `git` | Clone, branch, commit, push |
| `gh` (GitHub CLI) | Open PRs |
| `aws` (AWS CLI v2) | Available for use_aws if needed |
| `node` 20 + `npm` | Run `npm test` in JS repos |
| `python` 3.12 + `pytest` | Run `pytest` in Python repos |

## Pipeline: `agent.py`

The entrypoint is a deterministic sequential script:

```
receive TicketPayload (JSON)
  → clone repo (token-auth URL)
  → create fix branch
  → FOR attempt IN 1..MAX_FIX_ATTEMPTS:
      → run kiro-cli (brief = description + optional failure context)
      → run tests (auto-detect: npm/mvn/pytest/make)
      → if pass: BREAK
      → if fail: reset working tree, retry
  → commit (message: "fix: {summary} [{ticket_id}]")
  → push fix branch
  → open PR (gh pr create)
  → return {status, pr_url, attempts}
```

### Key behaviors

- **Auto-detect test runner**: checks for `package.json`, `pom.xml`, `Makefile`, `pytest.ini`, `pyproject.toml` in priority order.
- **Retry with context**: on test failure, the next kiro-cli invocation includes the failure output so it can learn from the previous attempt.
- **Clean retry**: working tree is reset between attempts so kiro starts fresh (no compounding bad edits).
- **No changes = failure**: if kiro-cli produces no diff after all attempts, reports failure rather than opening an empty PR.
- **Configurable**: `MAX_FIX_ATTEMPTS` (default 3) is an env var.

## Invocation contract

The orchestrator sends the **TicketPayload as JSON** directly (not a rendered prompt):

```python
client = boto3.client("bedrock-agentcore", region_name="us-east-1")

response = client.invoke(
    runtimeArn=os.environ["AGENTCORE_RUNTIME_ARN"],
    runtimeSessionId=session_id,   # >= 33 chars
    payload=json.dumps(ticket_payload).encode("utf-8"),
)
```

The Runtime's `agent.py` receives this payload in its `@app.entrypoint` handler and runs the pipeline.

### Payload schema (unchanged from harness era)

```json
{
  "ticket_id": "PROJ-15",
  "summary": "[SonarQube][P0] XSS — Swig template auto-escaping disabled",
  "description": "<full Jira description>",
  "ticket_url": "https://your-org.atlassian.net/browse/PROJ-15",
  "repo_clone_url": "https://github.com/your-org/your-repo.git",
  "base_branch": "master",
  "fix_branch": "sonarqube-fix/xss-swig-autoescape",
  "file_to_fix": "server.js:135",
  "sonar_hash": "6f50f92a-65f2-4c4c-aae5-9b069b44430a"
}
```

## Deployment

### Prerequisites

- AWS account with AgentCore enabled
- `GITHUB_TOKEN` and `KIRO_API_KEY` available as Runtime environment variables
- ECR repository (auto-created by `agentcore launch`)

### Deploy

```bash
cd runtime
export AWS_ACCOUNT_ID=123456789012
agentcore launch
```

This uses CodeBuild to build the ARM64 image, push to ECR, and deploy the Runtime. Takes ~1-2 minutes.

### The Runtime ARN

Deployment produces an ARN:

```
arn:aws:bedrock-agentcore:us-east-1:<account>:runtime/kiro-minions
```

Set this as `AGENTCORE_RUNTIME_ARN` in the orchestrator's environment.

## Secrets / env vars

| Var | Injected into | Purpose |
|-----|---------------|---------|
| `GITHUB_TOKEN` | Runtime container | Clone, push, PR creation |
| `KIRO_API_KEY` | Runtime container | kiro-cli headless authentication |
| `MAX_FIX_ATTEMPTS` | Runtime container | Retry budget (default: 3) |

These live with the Runtime, not the orchestrator. Configure them via AgentCore Runtime environment settings or SSM Parameter Store lookup in the container.

## Observability

- **Structured logging**: all pipeline steps log to stdout with timestamps and step markers.
- **OpenTelemetry**: the container entrypoint wraps `agent.py` with `opentelemetry-instrument` for distributed tracing.
- **AgentCore observability**: enabled in `.bedrock_agentcore.yaml` — traces appear in CloudWatch/X-Ray.

## Future evolution (B2)

If we find that the deterministic retry loop is insufficient for complex failures, we can evolve to B2 by adding a model call **only** for the error-recovery step:

```python
if not tests_passed and attempt == MAX_FIX_ATTEMPTS:
    # Ask a model to diagnose the failure before giving up
    diagnosis = call_model(f"Diagnose this test failure: {test_output}")
    # ... decide whether to retry differently or report failure
```

This keeps the happy path deterministic while adding intelligence only where it's needed.
