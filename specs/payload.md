# Ticket Payload

What the orchestrator passes to the coding agent.

```json
{
  "ticket_id": "SQ-42",
  "summary": "Unused variable 'result' in parser.js",
  "description": "Remove unused variable on line 47. SonarQube rule javascript:S1481.",
  "ticket_url": "https://yourorg.atlassian.net/browse/SQ-42",
  "repo_url": "https://github.com/org/repo",
  "default_branch": "main"
}
```

### Required fields
| Field | Source |
|---|---|
| `ticket_id` | `issue.key` |
| `summary` | `issue.fields.summary` |
| `description` | `issue.fields.description` |
| `ticket_url` | constructed |
| `repo_url` | Jira custom field OR parsed from description |
| `default_branch` | Jira custom field OR default `"main"` |

### Fallback: structured block in description
If no custom Jira fields, parse from description:
```
REPO: https://github.com/org/repo
BRANCH: main
---
[rest of description]
```
