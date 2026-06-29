# Ticket Payload

The contract between orchestrator and coding agent. The orchestrator builds this from the Jira ticket; the agent consumes it and never sees Jira.

```json
{
  "ticket_id": "SW-42",
  "summary": "Unused variable 'result' in parser.js",
  "description": "Remove unused variable on line 47. SonarQube rule javascript:S1481.",
  "ticket_url": "https://chaiawsacct.atlassian.net/browse/SW-42",
  "repo_url": "https://github.com/org/repo",
  "default_branch": "main"
}
```

## Fields

| Field | Source | Notes |
|---|---|---|
| `ticket_id` | `issue.key` | also used for the branch name `fix/<ticket_id>` |
| `summary` | `issue.fields.summary` | first line of the kiro prompt |
| `description` | `issue.fields.description` | the fix instructions; `REPO:`/`BRANCH:` lines stripped |
| `ticket_url` | `{JIRA_BASE_URL}/browse/{ticket_id}` | for PR body backlink |
| `repo_url` | parsed from description | required |
| `default_branch` | parsed from description | optional, defaults to `main` |

## Description parsing convention

The repo target lives in the ticket description as machine-readable lines. The orchestrator reads them, then strips them so they don't pollute the kiro prompt.

```
REPO: https://github.com/org/repo
BRANCH: main
---
<human-readable description of what to fix and why>
```

- `REPO:` — **required**. If absent, the orchestrator rejects the ticket (no repo = nothing to do).
- `BRANCH:` — optional, defaults to `main`.
- Everything below `---` becomes `description` in the payload.

Parsing is line-prefix based and case-insensitive on the keys (`REPO:`, `repo:`). Keep it dumb — no YAML, no front-matter.
