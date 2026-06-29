"""Orchestrator Lambda — Jira webhook entry point.

Flow:
  API Gateway (HTTP API) POST /webhook
    -> validate label (sonarqube / SONARQUBE-FIX)
    -> parse description Part 1 header + sonar_hash label
    -> idempotency check / claim via DynamoDB (key = sonar_hash)
    -> build TicketPayload
    -> render task prompt from payload
    -> invoke AgentCore harness (bedrock-agentcore invoke_harness)
    -> 200 / 400 / 500
"""

from __future__ import annotations

import json
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from parser import (
    extract_sonar_hash,
    has_sonarqube_label,
    parse_description_header,
)

AGENTCORE_HARNESS_ARN = os.environ.get("AGENTCORE_HARNESS_ARN", "")
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "kiro-minions-jobs")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Job-state values stored in DynamoDB.
STATUS_IN_FLIGHT = "in_flight"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Statuses that block a re-dispatch (idempotency).
_BLOCKING_STATUSES = {STATUS_IN_FLIGHT, STATUS_DONE}

_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_agentcore = boto3.client("bedrock-agentcore", region_name=AWS_REGION)


def _response(status_code: int, body: dict) -> dict:
    """Build an API Gateway (proxy) response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _extract_body(event: dict) -> dict:
    """Pull and JSON-parse the request body from an API Gateway event.

    Handles both raw-dict events (direct invoke) and API Gateway proxy events
    where the body is a JSON string (optionally base64-encoded).
    """
    if event is None:
        raise ValueError("empty event")

    # Direct invocation already shaped like a Jira webhook body.
    if "issue" in event:
        return event

    body = event.get("body")
    if body is None:
        raise ValueError("event has no 'body'")

    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")

    if isinstance(body, str):
        return json.loads(body)
    if isinstance(body, dict):
        return body
    raise ValueError(f"unsupported body type: {type(body)!r}")


def _claim_job(sonar_hash: str, ticket_id: str) -> bool:
    """Atomically claim a job for this sonar_hash.

    Returns True if we acquired the claim (new or previously-failed), False if a
    blocking record (in_flight / done) already exists -> caller should skip.
    """
    table = _dynamodb.Table(DYNAMODB_TABLE)
    now = int(time.time())
    item = {
        "sonar_hash": sonar_hash,
        "ticket_id": ticket_id,
        "status": STATUS_IN_FLIGHT,
        "created_at": now,
        "updated_at": now,
    }
    # Condition: item does not exist, OR its status is "failed" (retry allowed).
    cond = (
        "attribute_not_exists(sonar_hash) OR #s = :failed"
    )
    try:
        table.put_item(
            Item=item,
            ConditionExpression=cond,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":failed": STATUS_FAILED},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _mark_status(sonar_hash: str, status: str, extra: dict | None = None) -> None:
    """Update a job record's status (best-effort)."""
    table = _dynamodb.Table(DYNAMODB_TABLE)
    expr_names = {"#s": "status", "#u": "updated_at"}
    expr_values = {":s": status, ":u": int(time.time())}
    set_parts = ["#s = :s", "#u = :u"]
    if extra:
        for i, (k, v) in enumerate(extra.items()):
            expr_names[f"#e{i}"] = k
            expr_values[f":e{i}"] = v
            set_parts.append(f"#e{i} = :e{i}")
    try:
        table.update_item(
            Key={"sonar_hash": sonar_hash},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError:
        # Non-fatal: status bookkeeping should never break the request path.
        pass


def _build_payload(ticket_id: str, summary: str, description: str,
                   header: dict, sonar_hash: str) -> dict:
    """Assemble the TicketPayload contract object."""
    ticket_url = (
        f"{JIRA_BASE_URL}/browse/{ticket_id}" if JIRA_BASE_URL and ticket_id else ""
    )
    return {
        "ticket_id": ticket_id,
        "summary": summary,
        "description": description,
        "ticket_url": ticket_url,
        "repo_clone_url": header["repo_clone_url"],
        "base_branch": header["base_branch"],
        "fix_branch": header["fix_branch"],
        "file_to_fix": header["file_to_fix"],
        "sonar_hash": sonar_hash,
    }


def _render_task_prompt(payload: dict) -> str:
    """Render the TicketPayload into the harness task prompt.

    The harness runs a model in a ReAct loop, so it needs a natural-language
    brief, not a JSON argument. The full description is embedded verbatim so the
    agent has the complete SonarQube report (impact, rule, recommended fix).
    See specs/payload.md -> "Task prompt construction".
    """
    return (
        "You are an autonomous software engineer. Fix the SonarQube security "
        "issue described below.\n\n"
        "## Task\n"
        f"- Clone: {payload['repo_clone_url']} "
        "(use GITHUB_TOKEN env var for auth: "
        "https://x-token:$GITHUB_TOKEN@github.com/...)\n"
        f"- Base branch: {payload['base_branch']}\n"
        f"- Create fix branch: {payload['fix_branch']}\n"
        f"- Target file: {payload['file_to_fix']}\n\n"
        "## SonarQube Issue\n"
        f"{payload['description']}\n\n"
        "## Instructions\n"
        "1. Clone the repo and check out the base branch\n"
        "2. Create the fix branch\n"
        "3. Run kiro-cli to apply the fix: pipe the issue description to "
        "kiro-cli chat --no-interactive --trust-all-tools\n"
        "4. Run the test suite (auto-detect: npm test / mvn test / pytest / "
        "make test)\n"
        "5. If tests fail, review the failure and iterate with kiro-cli until "
        "they pass\n"
        f"6. Commit all changes with message: \"fix: {payload['summary']} "
        f"[{payload['ticket_id']}]\"\n"
        "7. Push the branch\n"
        f"8. Open a PR against {payload['base_branch']} using the GitHub CLI or "
        f"API. Include: ticket URL {payload['ticket_url']}, sonar_hash "
        f"{payload['sonar_hash']}\n\n"
        "You have shell access. Use it. Work until the PR is open."
    )


def _invoke_agent(payload: dict) -> str:
    """Invoke the AgentCore managed harness with a rendered task prompt.

    Calls ``invoke_harness`` (not ``invoke_agent``): the harness is a stateful
    model-in-a-loop runtime that drives git/kiro/tests/PR itself via its
    built-in ``shell`` tool. Returns the aggregated streamed text for logging.
    """
    if not AGENTCORE_HARNESS_ARN:
        raise RuntimeError("AGENTCORE_HARNESS_ARN env var is not set")

    # runtimeSessionId must be >= 33 chars. Derive from sonar_hash + uuid for
    # traceability while guaranteeing the length/uniqueness requirements.
    session_id = f"kiro-minions-{payload['sonar_hash']}-{uuid.uuid4().hex}"

    task_prompt = _render_task_prompt(payload)

    resp = _agentcore.invoke_harness(
        harnessArn=AGENTCORE_HARNESS_ARN,
        runtimeSessionId=session_id,
        messages=[
            {
                "role": "user",
                "content": [{"text": task_prompt}],
            }
        ],
    )

    # invoke_harness returns a streaming response; aggregate text for logging.
    chunks: list[str] = []
    for event in resp.get("completion", resp.get("stream", [])):
        chunk = event.get("chunk") if isinstance(event, dict) else None
        if chunk and "bytes" in chunk:
            chunks.append(chunk["bytes"].decode("utf-8", errors="replace"))
        elif isinstance(event, dict) and "text" in event:
            chunks.append(event["text"])
    return "".join(chunks)


def handler(event, context):  # noqa: ANN001 - Lambda signature
    """Lambda entry point."""
    # --- Parse incoming webhook body ---
    try:
        body = _extract_body(event)
    except (ValueError, json.JSONDecodeError) as exc:
        return _response(400, {"error": f"invalid request body: {exc}"})

    issue = body.get("issue") or {}
    fields = issue.get("fields") or {}
    ticket_id = issue.get("key") or ""
    summary = fields.get("summary") or ""
    description = fields.get("description") or ""
    labels = fields.get("labels") or []

    # --- Validate label gate ---
    if not has_sonarqube_label(labels):
        return _response(
            200,
            {"status": "ignored", "reason": "missing sonarqube/SONARQUBE-FIX label"},
        )

    # --- Extract idempotency key ---
    sonar_hash = extract_sonar_hash(labels)
    if not sonar_hash:
        return _response(400, {"error": "missing sonar-hash:<key> label"})

    # --- Parse description Part 1 header ---
    try:
        header = parse_description_header(description)
    except ValueError as exc:
        return _response(400, {"error": str(exc)})

    # --- Idempotency claim ---
    try:
        claimed = _claim_job(sonar_hash, ticket_id)
    except ClientError as exc:
        return _response(500, {"error": f"dynamodb error: {exc}"})

    if not claimed:
        return _response(
            200,
            {
                "status": "skipped",
                "reason": "job already in_flight or done for this sonar_hash",
                "sonar_hash": sonar_hash,
            },
        )

    # --- Build payload and dispatch to the coding agent ---
    payload = _build_payload(ticket_id, summary, description, header, sonar_hash)

    try:
        completion = _invoke_agent(payload)
    except Exception as exc:  # noqa: BLE001 - report any dispatch failure
        _mark_status(sonar_hash, STATUS_FAILED, {"error": str(exc)})
        return _response(500, {"error": f"agent invocation failed: {exc}"})

    return _response(
        200,
        {
            "status": "dispatched",
            "sonar_hash": sonar_hash,
            "ticket_id": ticket_id,
            "agent_response": completion,
        },
    )
