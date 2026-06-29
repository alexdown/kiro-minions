# Ticket Payload

The contract between orchestrator and coding agent. The orchestrator builds this from the Jira webhook body; the agent consumes it and never sees Jira.

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
