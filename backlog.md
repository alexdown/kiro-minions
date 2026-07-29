# Backlog

## Project Summary

**kiro-minions** is an autonomous SonarQube remediation pipeline: a labelled Jira ticket goes in, a GitHub PR comes out, with no human in the loop between the two.

The system consists of:

- **Orchestrator** (Python Lambda behind API Gateway): receives Jira webhooks, validates labels (`sonarqube` / `SONARQUBE-FIX`), parses the ticket description header (clone URL, base branch, fix branch, target file), extracts `sonar-hash` for idempotency (DynamoDB conditional put), renders a natural-language task prompt, and invokes an AWS Bedrock AgentCore managed harness via `invoke_harness`.
- **Coding Agent** (AgentCore managed harness): a stateful model-in-a-loop runtime (Claude Sonnet/Opus on Bedrock) with built-in `shell` and `file_operations` tools. It receives the task prompt and autonomously clones the repo, applies a fix using `kiro-cli` headless, runs tests (iterating on failure), commits, pushes, and opens a GitHub PR.
- **Infrastructure** (AWS SAM): API Gateway HTTP API, Lambda, DynamoDB table for job state, IAM policies for AgentCore and DynamoDB.

The ticket description is the contract: Part 1 is a machine-readable header block, Part 2 is the full SonarQube issue report (description, impact, rule, recommended fix). The orchestrator needs no Jira API credentials; everything arrives in the webhook body.

---

## Backlog Items

### Security

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| S1 | Webhook signature validation | Validate Jira webhook signatures (HMAC or shared secret) before processing. Currently any POST to the endpoint is accepted, allowing spoofed invocations. | High | Small |
| S2 | Input sanitization on description fields | Parsed header values (clone URL, branch names) are passed directly to the agent prompt and shell commands. Validate URL scheme (https only), reject branch names with shell metacharacters, and whitelist patterns. | High | Small |
| S3 | Rate limiting / throttling | Add API Gateway throttling (requests per second/burst) to prevent abuse or accidental webhook storms from overwhelming the system. | Medium | Small |
| S4 | Restrict allowed repositories | Maintain an allow-list of GitHub organizations or repositories that can be targeted. A crafted ticket could currently point to any repo the GITHUB_TOKEN has access to. | High | Small |
| S5 | Least-privilege GITHUB_TOKEN scope | Document and enforce minimum required scopes for the token (repo contents write, PR create). Consider fine-grained PATs scoped to specific repos. | Medium | Small |
| S6 | Secrets rotation strategy | Define a rotation plan for GITHUB_TOKEN and KIRO_API_KEY stored in the AgentCore Identity Token Vault. Add alerting on expiry. | Low | Medium |
| S7 | WAF integration | Place AWS WAF in front of API Gateway to filter malicious payloads, enforce request size limits, and block suspicious patterns. | Low | Medium |

---

### Reliability

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| R1 | Dead-letter queue for failed invocations | Add an SQS DLQ to capture webhook events that fail processing. Enables investigation and replay without data loss. | High | Small |
| R2 | Agent timeout and circuit breaker | Harness sessions are long-lived with no upper bound. Implement a maximum session duration (configurable, e.g. 30 min) and mark the job as failed if exceeded. | High | Medium |
| R3 | Retry with exponential backoff | When `invoke_harness` fails transiently (throttling, service unavailable), retry with jitter before marking the job failed. Consider Step Functions for orchestration. | Medium | Medium |
| R4 | DynamoDB TTL for stale in-flight jobs | Add a TTL attribute so in-flight records that are never resolved (e.g., harness crashed) expire and become retryable after a configurable period. | High | Small |
| R5 | Graceful degradation on DynamoDB failures | If DynamoDB is unreachable, the handler currently returns 500 for every request. Consider a fallback path (log and skip vs. fail-open vs. fail-closed) with explicit documentation of the chosen behavior. | Medium | Small |
| R6 | Webhook replay support | Store raw webhook payloads (S3 or DynamoDB) for a retention window, enabling manual replay of failed or skipped events for debugging. | Low | Medium |
| R7 | Health check endpoint | Add a `GET /health` route that verifies DynamoDB connectivity and harness ARN is configured. Useful for monitoring and deployment verification. | Low | Small |

---

### Observability

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| O1 | Structured logging (JSON) | Replace print/implicit Lambda logging with structured JSON logs (e.g., `aws-lambda-powertools`). Include `sonar_hash`, `ticket_id`, and `session_id` as correlation fields. | High | Small |
| O2 | CloudWatch metrics and dashboards | Emit custom metrics: webhooks received, jobs dispatched, jobs skipped (idempotent), jobs failed. Build a dashboard for operational visibility. | High | Medium |
| O3 | CloudWatch alarms | Alert on: high failure rate, invocation errors, DynamoDB throttling, elevated latency. Send to SNS or PagerDuty. | High | Small |
| O4 | Distributed tracing (X-Ray) | Enable AWS X-Ray tracing across API Gateway, Lambda, and DynamoDB to identify bottlenecks and trace individual webhook flows end-to-end. | Medium | Small |
| O5 | Agent session outcome tracking | After invocation, poll or receive a callback indicating whether the harness session succeeded (PR opened) or failed. Update DynamoDB status accordingly. | High | Medium |
| O6 | Cost tracking per invocation | Tag AgentCore sessions with `ticket_id` and `sonar_hash` for cost attribution. Track model token usage per fix for budgeting. | Low | Medium |
| O7 | Audit log | Maintain an immutable audit trail of all dispatch decisions: ticket received, validation result, claim outcome, harness invocation, final status. | Medium | Medium |

---

### Testing

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| T1 | Handler unit tests | Write unit tests for `handler.py` covering: label validation, missing sonar-hash, valid dispatch (mocked boto3), idempotency skip, DynamoDB error handling, base64-encoded bodies. | High | Medium |
| T2 | Integration tests with mocked AWS | Use `moto` or `localstack` to test the full handler flow against real DynamoDB behavior (conditional puts, updates) without hitting AWS. | High | Medium |
| T3 | Task prompt rendering tests | Verify the rendered prompt template contains all expected fields, handles edge cases (None values, missing fix_branch), and produces valid instructions. | Medium | Small |
| T4 | End-to-end smoke test | A scripted test that sends a real-shaped webhook to a deployed stack and verifies a 200 response with `dispatched` status (can use a no-op harness for the agent side). | Low | Large |
| T5 | Contract tests for ticket description | Validate various real-world description formats (different SonarQube rule types, multi-file findings, descriptions with special characters) to prevent parser regressions. | Medium | Small |
| T6 | Load / stress testing | Use Artillery or Locust to simulate webhook bursts and verify the system handles concurrent claims correctly (no duplicate dispatches under load). | Low | Medium |
| T7 | Coverage reporting and enforcement | Add pytest-cov, generate coverage reports in CI, and enforce a minimum threshold (e.g., 80% on orchestrator/). | Medium | Small |

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
| D8 | CONTRIBUTING.md | Document how to set up the dev environment, run tests, deploy, and contribute. Cover the harness deployment separately since it is not code. | Low | Small |

---

### Extensibility

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| E1 | Support multiple scanner sources | Abstract the parser to handle findings from other tools (Snyk, Dependabot, CodeQL, Semgrep) in addition to SonarQube. Use a strategy/plugin pattern for different ticket formats. | Medium | Large |
| E2 | GitLab / Bitbucket support | Generalize the agent prompt and auth to support GitLab (MR creation) and Bitbucket (PR creation) in addition to GitHub. The clone URL already indicates the host. | Medium | Large |
| E3 | Jira status callback | After the agent completes (PR opened or failed), update the Jira ticket: transition status, add a comment with the PR link, or add a `fix-submitted` label. Requires Jira API credentials on the orchestrator side. | High | Medium |
| E4 | Configurable agent model | Allow the harness model (Sonnet vs. Opus) to be selected per-ticket based on finding severity or complexity. P0/Critical could use Opus; lower severity uses Sonnet to save cost. | Low | Medium |
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
| I2 | Infrastructure as Code for the harness | Script harness creation/update (agentcore CLI or CloudFormation custom resource) so the full stack is deployable from a single command. Currently the harness is a manual setup step. | High | Medium |
| I3 | Auto-scaling and concurrency limits | Set Lambda reserved concurrency to prevent runaway scaling. Align with AgentCore session limits and DynamoDB capacity. | Medium | Small |
| I4 | Backup and disaster recovery | Enable DynamoDB point-in-time recovery. Document the recovery procedure for the jobs table and harness configuration. | Low | Small |
| I5 | Cost guardrails | Set AWS Budgets alerts for AgentCore/Bedrock usage. Implement a daily/weekly dispatch cap to prevent cost overruns from webhook storms. | Medium | Small |
| I6 | Blue/green or canary deployments | Use SAM deployment preferences (canary/linear) for the Lambda function to safely roll out changes with automatic rollback on errors. | Low | Medium |
| I7 | Custom domain for webhook endpoint | Set up a custom domain (e.g., `minions.your-org.com/webhook`) with ACM certificate, instead of the raw API Gateway URL. Easier to configure in Jira and more professional. | Low | Small |

---

### Agent Quality and Effectiveness

| # | Title | Description | Priority | Effort |
|---|-------|-------------|----------|--------|
| A1 | Fix success rate tracking | Track how often the agent's PR passes CI, gets approved, and gets merged. Build a feedback loop to identify which SonarQube rules the agent handles well vs. poorly. | High | Medium |
| A2 | Agent iteration budget | Limit the number of kiro-cli retry loops the agent can attempt before giving up. Prevents infinite loops on unfixable issues and controls cost. | Medium | Small |
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
