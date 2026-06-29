# Harness

The coding agent is an **AWS Bedrock AgentCore managed harness** — a stateful agent runtime that runs a model in a ReAct loop with built-in tools. This document covers how to create, configure, and deploy it, and what the orchestrator needs from it.

There is **no application code** to write or containerize. The harness *is* the agent: a configuration plus a system prompt. The model figures out the steps at runtime using the built-in `shell` tool.

## What the harness provides

- **A model in a loop.** Each session runs a Bedrock model (Claude Sonnet or Opus) in a ReAct loop: think → call a tool → observe → think again → repeat, until the model decides it's done.
- **Built-in tools, always available:**
  - `shell` — run arbitrary shell commands (`git`, `kiro-cli`, `npm test`, `gh pr create`, …).
  - `file_operations` — read/write/edit files in the session filesystem.
- **Isolated microVM per session** — its own filesystem and shell; sessions don't share state.
- **Long-lived sessions** — no 600s timeout. The agent works until the PR is open.

Because git, kiro, and testing all run through `shell`, we need **no custom tools** and **no custom container**. The default harness environment already has shell access.

## Configuration

| Setting | Value | Notes |
|---|---|---|
| Name | `kiro-minions-agent` | |
| Model | `anthropic.claude-sonnet-...` (or Opus) on Bedrock | Opus for harder fixes |
| Built-in tools | `shell`, `file_operations` | default, always available |
| `allowedTools` | `["shell", "file_*"]` | restrict to what's needed |
| System prompt | `agent/system-prompt.md` | persona + approach |
| Env vars | `GITHUB_TOKEN`, `KIRO_API_KEY` | see "Secrets" below |

### `allowedTools`

Restrict the harness to only the tools it needs:

```json
"allowedTools": ["shell", "file_*"]
```

`shell` covers git, kiro-cli, the test runners, and the GitHub CLI. `file_*` covers the `file_operations` family. No other tools are required.

### Secrets / env vars

`GITHUB_TOKEN` and `KIRO_API_KEY` must be available **inside the session** as environment variables. Two supported mechanisms:

1. **AgentCore Identity Token Vault (recommended).** Store the secrets in the vault and reference them in the harness config; AgentCore injects them into the session environment. Secrets never transit the orchestrator.
2. **Injected at invoke time.** Pass them through the invocation environment/config. Simpler for a demo, but the orchestrator then handles the secrets.

The harness owns these secrets — the orchestrator does **not** need `GITHUB_TOKEN` or `KIRO_API_KEY`.

## Creating the harness

### Option A — agentcore CLI

```bash
agentcore create \
  --name kiro-minions-agent \
  --model-provider bedrock \
  --model anthropic.claude-sonnet-4-... \
  --system-prompt-file agent/system-prompt.md \
  --allowed-tools shell,file_*
```

The CLI uses a project config file (`agent/harness.json`, see below) for the full set of settings. Deploying produces the **harness ARN**.

### Option B — boto3 control plane

Use the `bedrock-agentcore-control` client's `create_harness`:

```python
import boto3, json

control = boto3.client("bedrock-agentcore-control", region_name="us-east-1")

with open("agent/system-prompt.md") as f:
    system_prompt = f.read()

resp = control.create_harness(
    name="kiro-minions-agent",
    modelProvider="bedrock",
    modelId="anthropic.claude-sonnet-4-...",
    systemPrompt=system_prompt,
    allowedTools=["shell", "file_*"],
    # env vars wired via Identity Token Vault references
)
harness_arn = resp["harnessArn"]
```

## The harness ARN

Creating the harness yields an ARN like:

```
arn:aws:bedrock-agentcore:us-east-1:<account>:harness/kiro-minions-agent
```

This ARN is the **only thing the orchestrator needs**. Set it as the orchestrator's `AGENTCORE_HARNESS_ARN` env var. The orchestrator passes it to `invoke_harness`:

```python
client = boto3.client("bedrock-agentcore", region_name="us-east-1")
response = client.invoke_harness(
    harnessArn=os.environ["AGENTCORE_HARNESS_ARN"],
    runtimeSessionId=session_id,   # UUID, >= 33 chars
    messages=[{"role": "user", "content": [{"text": task_prompt}]}],
)
```

(Note: the orchestrator calls `invoke_harness` on the **`bedrock-agentcore`** data-plane client; harness *creation* uses the **`bedrock-agentcore-control`** client.)

## Invocation contract (what the orchestrator sends)

- `harnessArn` — from `AGENTCORE_HARNESS_ARN`.
- `runtimeSessionId` — fresh UUID per ticket, **≥ 33 characters**. Derive from `sonar_hash` + UUID suffix for traceability.
- `messages` — a single user message whose `content[0].text` is the rendered **task prompt** (see [payload.md](payload.md) → "Task prompt construction").

The response is a **streaming** response. The orchestrator aggregates it for logging/inspection but does not parse a structured result — success is observable as the opened PR.

## Why this shape

The previous design treated the agent as a Lambda-style function (input → run fixed Python steps → output). That fought the platform: it hardcoded the clone/branch/kiro/test/PR sequence and bolted on retry logic. The harness model is strictly better here:

- The agent **iterates on its own** when kiro produces a bad fix or tests fail — no retry code to maintain.
- **No container** to build, scan, or keep patched.
- **No 600s timeout** — long fixes just keep running.
- The "steps" live as **instructions in a prompt**, trivially editable without a redeploy of code.

The harness provides the environment; the model provides the engineering.
