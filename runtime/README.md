# Runtime — AgentCore Custom Container

This is the coding agent: a **deterministic Python pipeline** running in an AgentCore Runtime custom container. No outer model loop — the only AI reasoning is inside `kiro-cli`.

See [../specs/runtime.md](../specs/runtime.md) for the full design.

## Contents

| File | What it is |
|------|------------|
| `Dockerfile` | ARM64 container image: kiro-cli, gh, git, AWS CLI, Node.js, Python |
| `agent.py` | Deterministic pipeline: clone → branch → kiro-cli → test → retry → commit → push → PR |
| `requirements.txt` | Python deps (bedrock-agentcore, boto3) |
| `.bedrock_agentcore.yaml` | AgentCore starter toolkit deployment config |
| `.dockerignore` | Files excluded from the Docker build context |

## How it works

1. The SQS consumer Lambda sends a **TicketPayload** (JSON) to this Runtime via `bedrock-agentcore:Invoke`.
2. `agent.py` receives the payload in its `@app.entrypoint` handler.
3. It runs a fixed sequence: configure gh auth (token never in URL/argv), clone, branch, kiro-cli fix, test, retry loop, commit, push, PR.
4. It writes its terminal job status (`done` + `pr_url` / `failed` / `tests_failed`) to the shared DynamoDB jobs table.
5. Returns a JSON result: `{status, pr_url, tests_passed, attempts}` (`attempts` is the real kiro-cli attempt count).

## Deploy

```bash
cd runtime
export AWS_ACCOUNT_ID=123456789012
agentcore launch
```

This builds the ARM64 image via CodeBuild, pushes to ECR, and deploys the Runtime (~1-2 min).

## Environment variables

| Var | Required | Purpose |
|-----|----------|---------|
| `GITHUB_TOKEN` | Yes | Git clone/push, gh pr create (via the gh credential helper — never in the clone URL/argv) |
| `KIRO_API_KEY` | Yes | kiro-cli headless authentication |
| `MAX_FIX_ATTEMPTS` | No | Retry budget (default: 3) |
| `DYNAMODB_TABLE` | Yes | Shared jobs table the runtime writes its terminal status to |
| `REPO_ALLOWLIST` | No | Owner/repo globs re-checked before cloning (defence in depth; empty = allow all) |

## Local testing

```bash
# Build locally (requires Docker with ARM64 support or buildx)
docker build --platform linux/arm64 -t kiro-minions-runtime .

# Run with a test payload
echo '{"repo_clone_url":"...","base_branch":"main",...}' | \
  docker run --rm -i \
    -e GITHUB_TOKEN=ghp_... \
    -e KIRO_API_KEY=... \
    kiro-minions-runtime \
    python -c "from agent import run_pipeline; import json,sys; print(json.dumps(run_pipeline(json.load(sys.stdin))))"
```
