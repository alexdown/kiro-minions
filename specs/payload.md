# Ticket Payload

The contract the orchestrator builds from the Jira webhook body. It is the orchestrator's internal representation of the ticket; the orchestrator then renders it into the **task prompt** that is sent to the AgentCore harness (see "Task prompt construction" below). The agent never sees Jira and never receives this JSON directly — it receives the rendered prompt.

```json
{
  "ticket_id": "SW-15",
  "summary": "[SonarQube][P0] XSS — Swig template auto-escaping disabled (server.js:135)",
  "description": "<full verbatim Jira description — structured header block + inline SonarQube report>",
  "ticket_url": "https://chaiawsacct.atlassian.net/browse/SW-15",
  "repo_clone_url": "https://github.com/alexdown/NodeGoat.git",
  "base_branch": "master",
  "fix_branch": "sonarqube-fix/xss-swig-autoescape",
  "file_to_fix": "server.js:135",
  "sonar_hash": "6f50f92a-65f2-4c4c-aae5-9b069b44430a"
}
```

## Fields

| Field | Source | Notes |
|---|---|---|
| `ticket_id` | `issue.key` | e.g. `SW-15`; used in PR title/body backlink |
| `summary` | `issue.fields.summary` | the one-line title; not used to drive mechanics |
| `description` | `issue.fields.description` | **full, verbatim** — both parts. Becomes the kiro prompt context |
| `ticket_url` | `{JIRA_BASE_URL}/browse/{ticket_id}` | for PR body backlink |
| `repo_clone_url` | description Part 1 `Clone:` line | required; the `.git` clone URL |
| `base_branch` | description Part 1 `Base branch:` line | fetch from + open PR against this; defaults to `main` if absent |
| `fix_branch` | description Part 1 `Suggested fix branch:` line | branch the agent creates and pushes |
| `file_to_fix` | description Part 1 `File to fix:` line | e.g. `server.js:135`; focuses the agent's edit |
| `sonar_hash` | label `sonar-hash:<key>` | **idempotency key** (also present in body as `_sonar-hash:`) |

## Where each field comes from

Two sources in the webhook body: the **structured header** inside `description`, and the **labels** array.

### 1. Description structured block (Part 1)

The top of the Jira description is machine-readable, line-prefix based:

```
Repo URL: https://github.com/alexdown/NodeGoat
Clone: https://github.com/alexdown/NodeGoat.git
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

Parsing is line-prefix based and case-insensitive on the keys. Keep it dumb — no YAML, no front-matter. Lines not recognized are left alone.

**Important:** the header block is **not stripped** from `description`. The full description (header + inline SonarQube report below it) is passed verbatim to the agent as its prompt — the header is harmless context and the SonarQube report (Part 2) is the actual fix brief (description, impact, recommended fix, rule, references).

### 2. Labels → sonar_hash

The webhook's `issue.fields.labels` array carries the dedup marker:

```
["CWE-79", "OWASP-A03", "P0", "SONARQUBE-FIX", "XSS", "security",
 "sonar-hash:6f50f92a-65f2-4c4c-aae5-9b069b44430a", "sonarqube"]
```

The orchestrator finds the label starting with `sonar-hash:` and takes the value after the colon → `sonar_hash`. (The same hash also appears in the description body as `_sonar-hash: ...` — the label is the canonical source.)

## Idempotency

`sonar_hash` is the natural idempotency key — one finding, one hash, one PR.

Before invoking the agent, the orchestrator checks for an existing fix for this hash:

- an **open PR** for `fix_branch` (or whose body references the hash), or
- an **in-flight job** (job-state record keyed by `sonar_hash`, or an "in-progress" label on the ticket).

If either exists → **skip the dispatch**. This prevents duplicate PRs when SonarQube re-fires the webhook or the ticket is edited. On a fresh hash, record the in-flight state, then invoke.

## Task prompt construction

The `TicketPayload` is **not** sent to the agent as JSON. The harness runs a model in a loop, so it needs a **natural-language task prompt**, not a function argument. The orchestrator renders the payload into a single user message and sends it via `invoke_harness`.

### Rendering

The orchestrator fills this template from the payload fields:

```
You are an autonomous software engineer. Fix the SonarQube security issue described below.

## Task
- Clone: {repo_clone_url} (use GITHUB_TOKEN env var for auth: https://x-token:$GITHUB_TOKEN@github.com/...)
- Base branch: {base_branch}
- Create fix branch: {fix_branch}
- Target file: {file_to_fix}

## SonarQube Issue
{description}        # full verbatim Jira description — header block + inline SONAR-MCP-001 report with recommended fix

## Instructions
1. Clone the repo and check out the base branch
2. Create the fix branch
3. Run kiro-cli to apply the fix: pipe the issue description to
   kiro-cli chat --no-interactive --trust-all-tools
4. Run the test suite (auto-detect: npm test / mvn test / pytest / make test)
5. If tests fail, review the failure and iterate with kiro-cli until they pass
6. Commit all changes with message: "fix: {summary} [{ticket_id}]"
7. Push the branch
8. Open a PR against {base_branch} using the GitHub CLI or API.
   Include: ticket URL {ticket_url}, sonar_hash {sonar_hash}

You have shell access. Use it. Work until the PR is open.
```

Every placeholder maps directly to a `TicketPayload` field — `{repo_clone_url}`, `{base_branch}`, `{fix_branch}`, `{file_to_fix}`, `{description}`, `{summary}`, `{ticket_id}`, `{ticket_url}`, `{sonar_hash}`. The full `description` is embedded **verbatim** so the agent has the complete SonarQube report (impact, rule, recommended fix) as its brief.

### Invocation

The rendered prompt becomes the `text` of a single user message:

```python
import boto3, uuid

client = boto3.client("bedrock-agentcore", region_name="us-east-1")

task_prompt = render_task_prompt(payload)          # template above
session_id = uuid.uuid4().hex + uuid.uuid4().hex     # >= 33 chars

response = client.invoke_harness(
    harnessArn=os.environ["AGENTCORE_HARNESS_ARN"],
    runtimeSessionId=session_id,
    messages=[{
        "role": "user",
        "content": [{"text": task_prompt}],
    }],
)
# response is a streaming response — aggregate for logging.
```

Key points:

- The orchestrator calls **`invoke_harness`**, not `invoke_agent`.
- `runtimeSessionId` is a fresh UUID per ticket, **≥ 33 characters** (the orchestrator derives it from `sonar_hash` + a UUID suffix so the session is traceable).
- The mechanics (clone, branch, kiro, tests, retry-on-failure, commit, push, PR) are encoded **in the prompt as instructions** — the harness executes them with its built-in `shell` tool, deciding its own steps and iterating as needed. There is **no hardcoded step sequence or retry logic** in our code.
