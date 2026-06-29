# Coding Agent — AgentCore Managed Harness

This directory is **not a Python application**. The coding agent is an AWS Bedrock AgentCore *managed harness* — a stateful model-in-a-loop runtime with built-in `shell` and `file_operations` tools. There is no container to build and no agent code to run; the harness *is* the agent.

See [../specs/harness.md](../specs/harness.md) for the full design.

## Contents

| File | What it is |
|---|---|
| `harness.json` | AgentCore harness configuration (agentcore CLI project format): model, allowed tools, env vars. |
| `system-prompt.md` | The agent's system prompt / persona / standard procedure, loaded as the harness system prompt. |
| `README.md` | This file. |

## How it runs

1. The orchestrator builds a **task prompt** from the Jira ticket (see [../specs/payload.md](../specs/payload.md) → "Task prompt construction").
2. It calls `invoke_harness(harnessArn=..., runtimeSessionId=<uuid≥33chars>, messages=[{role:"user", content:[{text: task_prompt}]}])` on the `bedrock-agentcore` client.
3. The harness spins up an isolated microVM session and runs a Claude model in a ReAct loop, using the built-in `shell` tool to: clone → branch → `kiro-cli` → test (iterating on failure) → commit → push → open PR.
4. The agent decides when it's done — when the PR is open.

No git/kiro/test logic lives here as code. The mechanics are described as instructions in the task prompt; the model executes them.

## Deploy

### Prerequisites

- AWS account with Bedrock AgentCore enabled in your region (e.g. `us-east-1`).
- `GITHUB_TOKEN` and `KIRO_API_KEY` stored in the AgentCore Identity Token Vault (referenced by `harness.json`), or supplied at invoke time.
- The `agentcore` CLI installed, **or** use the boto3 control-plane client.

### Option A — agentcore CLI

```bash
cd agent
agentcore create \
  --name kiro-minions-agent \
  --model-provider bedrock \
  --system-prompt-file system-prompt.md \
  --allowed-tools shell,file_*
# (or simply: agentcore deploy   # uses harness.json as the project config)
```

### Option B — boto3 control plane

Use `bedrock-agentcore-control.create_harness(...)` with the values from `harness.json` and the contents of `system-prompt.md`. See [../specs/harness.md](../specs/harness.md#creating-the-harness).

### After deploy

Deployment yields the **harness ARN**:

```
arn:aws:bedrock-agentcore:us-east-1:<account>:harness/kiro-minions-agent
```

Give that ARN to the orchestrator as the `AGENTCORE_HARNESS_ARN` env var. That's the only wiring the orchestrator needs.
