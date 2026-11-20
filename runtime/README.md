# Runtime — AgentCore Custom Container

This is the coding worker: a **deterministic Python pipeline** running in an AgentCore Runtime custom container. No outer model loop — the only AI reasoning is inside `kiro-cli` (and inside the optional verification gates). The container ships two pipelines, selected by the payload's `task_type`: `one_shot` (`agent.py`) and `spec` (`spec_agent.py`).

See [../specs/runtime.md](../specs/runtime.md) and [../specs/spec-executor.md](../specs/spec-executor.md) for the full design.

## Contents

| File | What it is |
|------|------------|
| `Dockerfile` | ARM64 container image: kiro-cli, gh, git, AWS CLI, Node.js, Python |
| `agent.py` | `one_shot` pipeline: clone → branch → kiro-cli → test → retry → commit → push → PR |
| `spec_agent.py` | `spec` pipeline: run a whole `tasks.md` task-by-task (one commit per task, one PR per spec, stop-at-first-blocker) |
| `tasks_md.py` | Parser for a `tasks.md` checklist used by the `spec` pipeline |
| `review_gates.py` | Optional verification gates (adversarial review + verify-vs-spec) behind the default-off `SPEC_REVIEW_ENABLED` / `SPEC_VERIFY_SPEC_ENABLED` flags |
| `requirements.txt` | Python deps (bedrock-agentcore, boto3) |
| `.bedrock_agentcore.yaml` | AgentCore starter toolkit deployment config |
| `.dockerignore` | Files excluded from the Docker build context |

## How it works

1. The SQS consumer Lambda sends a **TaskPayload** (JSON) to this Runtime via `bedrock-agentcore:Invoke`.
2. The entrypoint handler receives the payload and dispatches on `task_type`: `agent.py` for `one_shot`, `spec_agent.py` for `spec`.
3. The `one_shot` pipeline runs a fixed sequence: configure gh auth (token never in URL/argv), clone, branch, kiro-cli fix, test, retry loop, commit, push, PR. The `spec` pipeline runs the same building blocks task-by-task over a whole `tasks.md`.
4. It writes its terminal job status (`done` + `pr_url` / `failed` / `tests_failed`) to the shared DynamoDB jobs table.
5. Returns a JSON result: `{status, pr_url, tests_passed, attempts}` (`attempts` is the real kiro-cli attempt count).

When `SPEC_REVIEW_ENABLED` / `SPEC_VERIFY_SPEC_ENABLED` are set, `review_gates.py` runs a post-step review phase (post-loop on the `spec` path, post-fix on the `one_shot` path) that can downgrade the PR to draft. The flags default off, so both pipelines are unchanged unless they are enabled.

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
| `SPEC_REVIEW_ENABLED` | No | Enable Gate A (adversarial review) in the post-step review phase. Default off. Gates both pipelines |
| `SPEC_VERIFY_SPEC_ENABLED` | No | Enable Gate B (verify-vs-spec). Default off. On the `one_shot` path Gate B is skip-with-notice |

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
