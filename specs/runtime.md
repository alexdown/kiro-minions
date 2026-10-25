# Runtime

The coding agent is a **deterministic Python script** running inside an **AgentCore Runtime custom container**. It replaces the previous managed-harness approach (two nested model loops) with a single-loop architecture where only `kiro-cli` performs AI reasoning.

## Why Runtime over Managed Harness?

> **Historical note.** The paragraphs below describe the **retired** managed-harness
> design purely as rationale for the current B1 architecture. The legacy harness
> definition is preserved, clearly marked `[LEGACY]`, under `agent/` and in
> [harness.md](harness.md). The deployed system no longer uses it.

The previous architecture used an AgentCore managed harness (a Strands-powered model-in-a-loop) that drove `kiro-cli` via its built-in `shell` tool. This created **two nested agent loops**:

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

(The project targets Python 3.12 via the Dockerfile and the SAM `Globals` runtime; the 3.11 interpreter used to run the unit-test suite is only the test host, not the deployed runtime.)

## Pipeline: `agent.py`

The entrypoint is a deterministic sequential script:

```
receive TicketPayload (JSON)
  → configure git auth (gh auth setup-git; token read from env, never in URL/argv)
  → re-check repo allowlist (defence in depth)
  → clone repo
  → create fix branch
  → FOR attempt IN 1..MAX_FIX_ATTEMPTS:
      → run kiro-cli (brief = description + optional failure context)
      → run tests (auto-detect: npm/mvn/pytest/make)
      → if pass: BREAK
      → if fail: reset working tree, retry
      → if the same failure fingerprint repeats: early-stop (repeated_failure)
  → commit (message: "fix: {summary} [{ticket_id}]")
  → push fix branch
  → open PR (gh pr create; DRAFT when no tests were detected)
  → write terminal job status to DynamoDB (done + pr_url / failed / tests_failed)
  → return {status, pr_url, tests_passed, attempts}
```

`attempts` reports the **actual** number of kiro-cli attempts (the loop variable), not `MAX_FIX_ATTEMPTS`.

### Key behaviors

- **Auto-detect test runner**: checks for `package.json`, `pom.xml`, `Makefile`, `pytest.ini`, `pyproject.toml` in priority order.
- **Retry with context**: on test failure, the next kiro-cli invocation includes the failure output so it can learn from the previous attempt.
- **Clean retry**: working tree is reset between attempts so kiro starts fresh (no compounding bad edits).
- **No changes = failure**: if kiro-cli produces no diff after all attempts, reports failure rather than opening an empty PR.
- **Repeated-failure early stop (#12)**: if the test failure fingerprint is identical across attempts, the loop stops early instead of burning the full retry budget on an unfixable issue.
- **No-tests draft PR (#8)**: if no test runner can be detected, the pipeline opens a **draft** PR for human review and records a `tests_failed` status rather than silently claiming success.
- **Configurable**: `MAX_FIX_ATTEMPTS` (default 3) is an env var.

### Asynchronous flow and terminal status

The Runtime is the back half of an asynchronous pipeline. The webhook producer Lambda validates, claims (DynamoDB lease), and enqueues the payload on the SQS `JobsQueue` (returning `202`); an SQS-triggered consumer Lambda invokes this Runtime. On completion the Runtime writes its **own terminal job status** to the shared DynamoDB jobs table via `write_job_status`:

- `done` (+ `pr_url`) — a PR was opened. Blocks re-dispatch.
- `failed` — the pipeline could not produce a usable change. Reclaimable.
- `tests_failed` — tests still failed, or no tests were detected (draft PR). Reclaimable.

The status write is best-effort (a DynamoDB error is logged with `redact()` and swallowed so it cannot mask the pipeline result). The `JobsQueue` has a dead-letter queue (`maxReceiveCount=3`) for messages the consumer cannot process.

### Security controls

- **Secret redaction** — `redact()` masks `GITHUB_TOKEN` / `KIRO_API_KEY` / `WEBHOOK_SECRET` in every log line, exception, and status write.
- **Repo allowlist re-check** — the runtime re-validates `owner/repo` against `REPO_ALLOWLIST` before cloning, even though the producer already checked (defence in depth).
- **No token in argv/URL** — git/gh auth uses the gh credential helper (`gh auth setup-git`); `GITHUB_TOKEN` is read from the environment and never appears in a clone URL or command argument.
- **`shell=False` for ticket-derived data** — subprocess calls built from ticket fields pass argument lists, never a shell string; branch names and clone URLs are validated first.
- **Brief via stdin** — the kiro-cli brief (the full ticket description) is passed on stdin, not as an argv argument.

## Invocation contract

The **consumer** Lambda (not the webhook producer) sends the **TicketPayload as JSON** directly (not a rendered prompt) after receiving it from the SQS `JobsQueue`:

```python
client = boto3.client("bedrock-agentcore", region_name="us-east-1")

response = client.invoke(
    runtimeArn=os.environ["AGENTCORE_RUNTIME_ARN"],
    runtimeSessionId=session_id,   # >= 33 chars
    payload=json.dumps(ticket_payload).encode("utf-8"),
)
```

The Runtime's `agent.py` receives this payload in its `@app.entrypoint` handler and runs the pipeline.

### Payload schema (unchanged since the legacy managed-harness design; see [harness.md](harness.md))

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
| `GITHUB_TOKEN` | Runtime container | Clone, push, PR creation (via the gh credential helper — never placed in the clone URL/argv) |
| `KIRO_API_KEY` | Runtime container | kiro-cli headless authentication |
| `MAX_FIX_ATTEMPTS` | Runtime container | Retry budget (default: 3) |
| `DYNAMODB_TABLE` | Runtime container | Shared jobs table the runtime writes its terminal status to (same table as the orchestrator) |
| `REPO_ALLOWLIST` | Runtime container | Owner/repo globs re-checked before cloning (defence in depth; same value as the orchestrator; empty = allow all) |

These live with the Runtime, not the orchestrator. Configure them via AgentCore Runtime environment settings or SSM Parameter Store lookup in the container. Because the runtime writes its own terminal job status, its execution role needs DynamoDB write access to the jobs table (see the project README for the exact IAM actions).

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
