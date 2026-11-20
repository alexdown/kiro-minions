# Task Payload

`TaskPayload` is the single contract both ingresses produce and both task shapes consume (the historical `TicketPayload`, generalised; see ADR-005). It is sent **as JSON** directly to the AgentCore Runtime container, which receives it and runs the deterministic pipeline selected by `task_type`.

The logical idempotency key is `task_id`, carried in the physical `sonar_hash` partition-key attribute on the shared table (ADR-004), so the claim/lease logic, the consumer, and the runtime are identical across ingresses. A Jira payload carries `source="jira"`; a Kiro IDE payload carries `source="kiro-ide"` with an `actor`. The `task_type` field selects the pipeline: `one_shot` (one brief, one fix, one PR; `runtime/agent.py`) or `spec` (a whole `tasks.md`; `runtime/spec_agent.py`).

The example below shows the fields the Jira ingress supplies; the IDE ingress produces the same contract with `source`/`actor` and (for a spec job) `tasks_md_path`/`tasks_md` (see [ide-ingress.md](ide-ingress.md) §5).

```json
{
  "ticket_id": "PROJ-15",
  "summary": "[SonarQube][P0] XSS — Swig template auto-escaping disabled (server.js:135)",
  "description": "<full verbatim Jira description — structured header block + inline SonarQube report>",
  "ticket_url": "https://your-org.atlassian.net/browse/PROJ-15",
  "repo_clone_url": "https://github.com/your-org/your-repo.git",
  "base_branch": "master",
  "fix_branch": "sonarqube-fix/xss-swig-autoescape",
  "file_to_fix": "server.js:135",
  "sonar_hash": "6f50f92a-65f2-4c4c-aae5-9b069b44430a"
}
```

## Fields

| Field | Source | Notes |
|---|---|---|
| `ticket_id` | `issue.key` | e.g. `PROJ-15`; used in commit message and PR title |
| `summary` | `issue.fields.summary` | One-line title; used in commit message |
| `description` | `issue.fields.description` | **Full, verbatim** — both parts. Becomes the kiro-cli brief |
| `ticket_url` | `{JIRA_BASE_URL}/browse/{ticket_id}` | Backlink in the PR body |
| `repo_clone_url` | description Part 1 `Clone:` line | Required; the `.git` clone URL |
| `base_branch` | description Part 1 `Base branch:` line | Clone from + open PR against; defaults to `main` |
| `fix_branch` | description Part 1 `Suggested fix branch:` line | Branch the pipeline creates and pushes |
| `file_to_fix` | description Part 1 `File to fix:` line | e.g. `server.js:135`; focuses kiro-cli's edit |
| `sonar_hash` | label `sonar-hash:<key>` | **Idempotency key** |

## Where each field comes from (Jira ingress)

For the Jira ingress, the fields are sourced from two places in the webhook body: the **structured header** inside `description`, and the **labels** array.

### 1. Description structured block (Part 1)

The top of the Jira description is machine-readable, line-prefix based:

```
Repo URL: https://github.com/your-org/your-repo
Clone: https://github.com/your-org/your-repo.git
Base branch: master
File to fix: server.js:135
Suggested fix branch: sonarqube-fix/xss-swig-autoescape
```

The orchestrator scans these lines and maps them:

| Line prefix | Payload field |
|---|---|
| `Clone:` | `repo_clone_url` (**required** — no clone URL = reject ticket) |
| `Base branch:` | `base_branch` (defaults to `main`) |
| `File to fix:` | `file_to_fix` |
| `Suggested fix branch:` | `fix_branch` |

Parsing is line-prefix based and case-insensitive on the keys. Lines not recognized are ignored.

**Important:** the header block is **not stripped** from `description`. The full description (header + inline SonarQube report below it) is passed verbatim to kiro-cli as its brief.

### 2. Labels → sonar_hash

The webhook's `issue.fields.labels` array carries the dedup marker:

```
["CWE-79", "OWASP-A03", "P0", "SONARQUBE-FIX", "XSS", "security",
 "sonar-hash:6f50f92a-65f2-4c4c-aae5-9b069b44430a", "sonarqube"]
```

The orchestrator finds the label starting with `sonar-hash:` and takes the value after the colon.

## Idempotency

`sonar_hash` is the natural idempotency key — one finding, one hash, one PR.

Before enqueueing, the producer Lambda atomically claims the `sonar_hash` in DynamoDB (writing `status=in_flight` with a numeric `lease_expires_at = now + LEASE_SECONDS`):

- If a record exists with status `done`, or `in_flight` **with a live lease** → **skip** (no duplicate PRs).
- If no record exists, or status is `failed` / `tests_failed`, or `in_flight` **with an expired lease** → **claim and dispatch** (allows retry; a crashed or timed-out consumer cannot wedge a hash forever).

The claimed payload is enqueued on the SQS `JobsQueue`; a consumer Lambda then invokes the Runtime, and the Runtime writes its own terminal status (`done` + `pr_url` / `failed` / `tests_failed`) back to the same table. DynamoDB TTL on `lease_expires_at` provides best-effort cleanup; the lease comparison in the claim is the authoritative reclaim gate.

## Invocation

The **consumer** Lambda (triggered by the SQS `JobsQueue`) sends the `TaskPayload` as JSON bytes to the AgentCore Runtime:

```python
import boto3, json, uuid, os

client = boto3.client("bedrock-agentcore", region_name="us-east-1")

session_id = f"kiro-minions-{payload['sonar_hash']}-{uuid.uuid4().hex}"

response = client.invoke(
    runtimeArn=os.environ["AGENTCORE_RUNTIME_ARN"],
    runtimeSessionId=session_id,
    payload=json.dumps(payload).encode("utf-8"),
)
```

Key points:

- The consumer calls **`invoke`** on the `bedrock-agentcore` client.
- `runtimeSessionId` is derived from `sonar_hash` + UUID for traceability, **≥ 33 characters**.
- The payload is the `TaskPayload` JSON: no prompt rendering, no natural-language template.
- The Runtime's entrypoint (`agent.py` for `one_shot`, `spec_agent.py` for `spec`) receives the payload directly in its `@app.entrypoint` handler.
