# Backlog

## Project Summary

**kiro-minions** is an autonomous SonarQube remediation pipeline: a labelled Jira ticket goes in, a GitHub PR comes out, with no human in the loop between the two.

The system is an **asynchronous, deterministic (B1) pipeline** — there is no outer model loop; the only AI reasoning happens inside `kiro-cli`. It consists of:

- **Orchestrator — webhook producer** (Python Lambda behind API Gateway): authenticates the webhook (HMAC / shared-secret, constant-time compare), validates labels (`sonarqube` / `SONARQUBE-FIX`), parses the ticket description header (clone URL, base branch, fix branch, target file) and the `sonar-hash` label, enforces a repo allowlist, atomically **claims** the `sonar_hash` with a DynamoDB lease (idempotency), builds the `TicketPayload`, **enqueues** it on SQS, and returns `202`.
- **Orchestrator — SQS consumer** (Python Lambda): triggered by the `JobsQueue` (with a DLQ, `maxReceiveCount=3`), it invokes the AgentCore Runtime with the `TicketPayload` JSON.
- **Coding Agent — AgentCore Runtime** (deterministic custom container): a sequential B1 pipeline — clone → create fix branch → run `kiro-cli` headless → run tests → retry (bounded, with repeated-failure early-stop) → commit → push → open a GitHub PR — that writes its **own terminal job status** (`done` + `pr_url` / `failed` / `tests_failed`) back to DynamoDB. Auth uses the gh credential helper so the token is never placed in the clone URL or argv.
- **Infrastructure** (AWS SAM): API Gateway HTTP API (with default-route throttling), two Lambdas (producer + consumer), an SQS `JobsQueue` + dead-letter queue, a DynamoDB table for job state/lease (TTL on `lease_expires_at`), and IAM policies for AgentCore, DynamoDB, and SQS.

Job statuses: `in_flight` (claimed, with a lease) / `done` (terminal, blocks re-dispatch) / `failed` (reclaimable) / `tests_failed` (reclaimable). A claim succeeds only when the row is absent, `failed`, `tests_failed`, or `in_flight` with an expired lease.

The ticket description is the contract: Part 1 is a machine-readable header block, Part 2 is the full SonarQube issue report (description, impact, rule, recommended fix). The orchestrator needs no Jira API credentials; everything arrives in the webhook body.

---

## Backlog Items

### Security

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| S1 | Webhook signature validation | **DONE** — `handler.verify_webhook` authenticates every webhook before body parsing/claim: HMAC (`x-hub-signature-256`) or shared-secret header, constant-time (`hmac.compare_digest`), fail-closed when `WEBHOOK_SECRET` set (401/403), fail-open when empty. | High | Small |
| S2 | Input sanitization on description fields | **DONE** — `parser.validate_clone_url` (https only) and `parser.validate_branch_name` (rejects shell metacharacters); the runtime re-validates in `agent.py`. Ticket-derived data uses `shell=False` argument lists. | High | Small |
| S3 | Rate limiting / throttling | **DONE** — API Gateway stage-level `DefaultRouteSettings` throttling (rate/burst) in `deploy/template.yaml`. | Medium | Small |
| S4 | Restrict allowed repositories | **DONE** — `parser.repo_allowed` (owner/repo globs); enforced in the producer (`handler`, 403) and re-checked in the runtime (defence in depth). | High | Small |
| S5 | Least-privilege GITHUB_TOKEN scope | Document and enforce minimum required scopes for the token (repo contents write, PR create). Consider fine-grained PATs scoped to specific repos. | Medium | Small |
| S6 | Secrets rotation strategy | Define a rotation plan for GITHUB_TOKEN and KIRO_API_KEY stored in the AgentCore Identity Token Vault. Add alerting on expiry. | Low | Medium |
| S7 | WAF integration | Place AWS WAF in front of API Gateway to filter malicious payloads, enforce request size limits, and block suspicious patterns. | Low | Medium |

---

### Reliability

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| R1 | Dead-letter queue for failed invocations | **DONE** — `deploy/template.yaml` defines `JobsDeadLetterQueue` and a `RedrivePolicy` on the `JobsQueue` with `maxReceiveCount=3`, so payloads the consumer cannot process are captured for investigation/replay. | High | Small |
| R2 | Runtime timeout and circuit breaker | Runtime invocations are long-lived. Beyond the per-job lease, add a hard maximum execution duration (configurable, e.g. 30 min) and a circuit breaker so repeated Runtime failures stop new dispatches. (The lease/DLQ bound stuck jobs but do not cap a single run's wall-clock time.) | High | Medium |
| R3 | Retry with exponential backoff | When the consumer's `invoke` of the Runtime fails transiently (throttling, service unavailable), retry with jitter before letting the message go to the DLQ. Consider Step Functions for orchestration. | Medium | Medium |
| R4 | DynamoDB TTL for stale in-flight jobs | **DONE** — `deploy/template.yaml` enables `TimeToLiveSpecification` on `lease_expires_at`; `handler._claim_job` reclaims an `in_flight` row once its lease has expired (the lease comparison is the authoritative gate; TTL is best-effort cleanup). | High | Small |
| R5 | Graceful degradation on DynamoDB failures | If DynamoDB is unreachable, the handler currently returns 500 for every request. Consider a fallback path (log and skip vs. fail-open vs. fail-closed) with explicit documentation of the chosen behavior. | Medium | Small |
| R6 | Webhook replay support | Store raw webhook payloads (S3 or DynamoDB) for a retention window, enabling manual replay of failed or skipped events for debugging. | Low | Medium |
| R7 | Health check endpoint | Add a `GET /health` route that verifies DynamoDB connectivity, SQS reachability, and that the Runtime ARN is configured. Useful for monitoring and deployment verification. | Low | Small |

---

### Observability

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| O1 | Structured logging (JSON) | Replace print/implicit Lambda logging with structured JSON logs (e.g., `aws-lambda-powertools`). Include `sonar_hash`, `ticket_id`, and `session_id` as correlation fields. | High | Small |
| O2 | CloudWatch metrics and dashboards | Emit custom metrics: webhooks received, jobs dispatched, jobs skipped (idempotent), jobs failed. Build a dashboard for operational visibility. | High | Medium |
| O3 | CloudWatch alarms | Alert on: high failure rate, invocation errors, DynamoDB throttling, elevated latency. Send to SNS or PagerDuty. | High | Small |
| O4 | Distributed tracing (X-Ray) | Enable AWS X-Ray tracing across API Gateway, Lambda, and DynamoDB to identify bottlenecks and trace individual webhook flows end-to-end. | Medium | Small |
| O5 | Agent session outcome tracking | **DONE** — the runtime writes its own terminal status via `agent.write_job_status` (`done` + `pr_url` / `failed` / `tests_failed`) to the shared jobs table; `handler` recognizes these statuses in its claim/reclaim logic. | High | Medium |
| O6 | Cost tracking per invocation | Tag AgentCore sessions with `ticket_id` and `sonar_hash` for cost attribution. Track model token usage per fix for budgeting. | Low | Medium |
| O7 | Audit log | Maintain an immutable audit trail of all dispatch decisions: ticket received, validation result, claim outcome, enqueue, Runtime invocation, final status. | Medium | Medium |

---

### Testing

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| T1 | Handler unit tests | **DONE** — `tests/test_handler.py`, `tests/test_handler_async.py`, `tests/test_handler_gaps.py`, `tests/test_webhook_auth.py`, `tests/test_repo_allowlist.py`, `tests/test_secret_redaction.py` cover label validation, missing sonar-hash, idempotent skip, DynamoDB error → 500, enqueue, consumer invoke, auth 401/403, allowlist 403, and base64 bodies. | High | Medium |
| T2 | Integration tests with mocked AWS | **DONE (via in-memory fakes)** — DynamoDB/SQS/AgentCore behavior (conditional puts, updates, send/receive, invoke) is exercised against the in-memory fakes/SDK stubs in `tests/fakes.py` rather than `moto`/`localstack`, keeping the suite zero-installs (real `boto3`/`moto` are not installable in this environment). `moto` remains pinned in `requirements-dev.txt` only as an optional real-SDK path. | High | Medium |
| T3 | Task prompt rendering tests | Verify the rendered prompt template contains all expected fields, handles edge cases (None values, missing fix_branch), and produces valid instructions. | Medium | Small |
| T4 | End-to-end smoke test | A scripted test that sends a real-shaped webhook to a deployed stack and verifies a `202` enqueue response (can use a no-op Runtime for the agent side). | Low | Large |
| T5 | Contract tests for ticket description | Validate various real-world description formats (different SonarQube rule types, multi-file findings, descriptions with special characters) to prevent parser regressions. | Medium | Small |
| T6 | Load / stress testing | Use Artillery or Locust to simulate webhook bursts and verify the system handles concurrent claims correctly (no duplicate dispatches under load). | Low | Medium |
| T7 | Coverage reporting and enforcement | **DONE** — `pytest-cov` + `coverage` pinned in `requirements-dev.txt`; `pyproject.toml` configures `[tool.coverage.*]` scoped to `orchestrator/` and `runtime/` with `fail_under=80` (CI: `pytest --cov=orchestrator --cov=runtime --cov-report=term-missing --cov-fail-under=80`). `--cov` is intentionally opt-in so the zero-install sandbox run still works. | Medium | Small |

---

### Developer Experience

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| D1 | CI/CD pipeline (GitHub Actions) | Automate lint (ruff/flake8), type-check (mypy), test (pytest), and deploy (SAM) on push/PR. Block merges on failure. | High | Medium |
| D2 | Type annotations on handler | Add full type annotations to `handler.py` functions. The `event` and `context` parameters are untyped; use `aws-lambda-powertools` typing or `TypedDict`. | Medium | Small |
| D3 | Local development with SAM CLI | Document and script `sam local invoke` / `sam local start-api` for local testing with sample events. Add sample event JSON fixtures. | Medium | Small |
| D4 | Pre-commit hooks | Configure pre-commit with ruff, mypy, and pytest to catch issues before push. | Low | Small |
| D5 | Makefile for common tasks | Add a `Makefile` with targets: `test`, `lint`, `typecheck`, `deploy`, `local`, `clean`. Standardize the dev workflow. | Low | Small |
| D6 | Dependency management (pyproject.toml) | Migrate from bare `requirements.txt` to `pyproject.toml` with optional dev dependencies (pytest, moto, ruff, mypy). Supports `pip install -e '.[dev]'`. | Medium | Small |
| D7 | Sample webhook payloads | Include fixture JSON files representing various Jira webhook shapes (create, update, different label sets) for manual and automated testing. | Medium | Small |
| D8 | CONTRIBUTING.md | Document how to set up the dev environment, run tests, deploy, and contribute. Cover the Runtime container deployment (`agentcore launch`) separately since it is built/deployed outside the SAM stack. | Low | Small |

---

### Extensibility

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| E1 | Support multiple scanner sources | Abstract the parser to handle findings from other tools (Snyk, Dependabot, CodeQL, Semgrep) in addition to SonarQube. Use a strategy/plugin pattern for different ticket formats. | Medium | Large |
| E2 | GitLab / Bitbucket support | Generalize the agent prompt and auth to support GitLab (MR creation) and Bitbucket (PR creation) in addition to GitHub. The clone URL already indicates the host. | Medium | Large |
| E3 | Jira status callback | After the agent completes (PR opened or failed), update the Jira ticket: transition status, add a comment with the PR link, or add a `fix-submitted` label. Requires Jira API credentials on the orchestrator side. | High | Medium |
| E4 | Configurable agent model | Allow the kiro-cli model (Sonnet vs. Opus) to be selected per-ticket based on finding severity or complexity. P0/Critical could use Opus; lower severity uses Sonnet to save cost. | Low | Medium |
| E5 | Multi-file findings | Support findings that span multiple files (e.g., taint flows across modules). The `File to fix` field currently takes a single path; extend to accept a list or directory scope. | Medium | Small |
| E6 | Custom fix strategies per rule | Allow repo owners to define custom fix templates or kiro personas per SonarQube rule (e.g., `S5247` always uses a specific approach). Support `.kiro/sonar-rules/` config in target repos. | Low | Large |
| E7 | Webhook filtering rules | Add configurable filters beyond just the `sonarqube` label: e.g., only process P0/P1, only specific CWE categories, only certain repos. Store rules in DynamoDB or SSM Parameter Store. | Medium | Medium |
| E8 | Batch processing | Support processing multiple SonarQube findings for the same repo in a single agent session (one PR with multiple fixes) to reduce overhead and PR noise. | Low | Large |
| E9 | PR review feedback loop | If the generated PR receives review comments requesting changes, trigger a new agent session to address the feedback and push follow-up commits. | Low | Large |
| E10 | Slack / Teams notifications | Notify a channel when a fix PR is opened or when the agent fails, with links to the PR and the original Jira ticket. | Low | Small |

---

### Infrastructure and Operations

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| I1 | Multi-environment support (dev/staging/prod) | Parameterize the SAM template for multiple environments. Use `samconfig.toml` profiles or separate parameter files. | Medium | Small |
| I2 | Infrastructure as Code for the Runtime | Script Runtime container build/deploy (`agentcore launch` or a CloudFormation custom resource) so the full stack — including the Runtime and its execution-role DynamoDB permissions — is deployable from a single command. Currently `agentcore launch` is a manual step outside the SAM stack. | High | Medium |
| I3 | Auto-scaling and concurrency limits | Set Lambda reserved concurrency to prevent runaway scaling. Align with AgentCore session limits and DynamoDB capacity. | Medium | Small |
| I4 | Backup and disaster recovery | Enable DynamoDB point-in-time recovery. Document the recovery procedure for the jobs table and the Runtime configuration. | Low | Small |
| I5 | Cost guardrails | Set AWS Budgets alerts for AgentCore/Bedrock usage. Implement a daily/weekly dispatch cap to prevent cost overruns from webhook storms. | Medium | Small |
| I6 | Blue/green or canary deployments | Use SAM deployment preferences (canary/linear) for the Lambda function to safely roll out changes with automatic rollback on errors. | Low | Medium |
| I7 | Custom domain for webhook endpoint | Set up a custom domain (e.g., `minions.your-org.com/webhook`) with ACM certificate, instead of the raw API Gateway URL. Easier to configure in Jira and more professional. | Low | Small |

---

### Agent Quality and Effectiveness

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| A1 | Fix success rate tracking | Track how often the agent's PR passes CI, gets approved, and gets merged. Build a feedback loop to identify which SonarQube rules the agent handles well vs. poorly. | High | Medium |
| A2 | Agent iteration budget | **DONE** — `runtime/agent.py` bounds the retry loop with `MAX_FIX_ATTEMPTS` (default 3) and early-stops on a repeated failure fingerprint (`repeated_failure`); the returned `attempts` reports the real attempt count. | Medium | Small |
| A3 | Pre-flight repo validation | Before dispatching to the agent, validate that the clone URL is accessible and the branch exists. Fail fast rather than burning an agent session on a broken ticket. | Medium | Small |
| A4 | Test suite detection heuristics | Improve the system prompt's test-detection guidance to handle monorepos, workspaces, and repos where tests require setup steps (docker-compose, database migrations). | Low | Medium |
| A5 | Fix verification before PR | Add an instruction for the agent to run the project's linter and type-checker (if present) in addition to tests, ensuring the fix does not introduce new warnings. | Medium | Small |
| A6 | PR quality standards | Enhance the prompt to produce better PR descriptions: include what was changed, why (link to CWE), testing performed, and risk assessment. Standardize the format. | Medium | Small |
| A7 | Agent telemetry collection | Capture agent session transcripts (tool calls, outputs, reasoning) for post-hoc analysis of agent behavior, debugging failures, and improving prompts. | Medium | Medium |
| A8 | Fallback to manual when agent fails | When the agent cannot fix the issue after exhausting retries, automatically add a comment to the Jira ticket explaining what was attempted and why it failed, so a human can pick it up. | Medium | Medium |

---

## Priority Legend

- **High**: Addresses a security gap, reliability risk, or blocks production readiness.
- **Medium**: Meaningful improvement for operability, maintainability, or capability, but not blocking.
- **Low**: Nice-to-have, future-looking, or only relevant at scale.

## Effort Legend

- **Small**: A few hours; isolated change, minimal dependencies.
- **Medium**: A day or two; touches multiple files or requires integration work.
- **Large**: Multiple days; significant design work, new subsystem, or cross-cutting change.
