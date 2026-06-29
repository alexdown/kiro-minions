"""Orchestrator Lambda — Jira webhook entry point.

Flow:
  API Gateway (HTTP API) POST /webhook
    -> validate label (sonarqube / SONARQUBE-FIX)
    -> parse description Part 1 header + sonar_hash label
    -> idempotency check / claim via DynamoDB (key = sonar_hash)
    -> build TicketPayload
    -> invoke AgentCore (bedrock-agent-runtime invoke_agent)
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

AGENTCORE_AGENT_ID = os.environ.get("AGENTCORE_AGENT_ID", "")
AGENTCORE_AGENT_ALIAS_ID = os.environ.get("AGENTCORE_AGENT_ALIAS_ID", "TSTALIASID")
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
_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)


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


def _invoke_agent(payload: dict) -> str:
    """Invoke the AgentCore coding agent with the TicketPayload as input.

    Returns the aggregated completion text from the streamed response.
    """
    if not AGENTCORE_AGENT_ID:
        raise RuntimeError("AGENTCORE_AGENT_ID env var is not set")

    session_id = f"kiro-minions-{payload['sonar_hash']}-{uuid.uuid4().hex[:8]}"
    resp = _agent_runtime.invoke_agent(
        agentId=AGENTCORE_AGENT_ID,
        agentAliasId=AGENTCORE_AGENT_ALIAS_ID,
        sessionId=session_id,
        inputText=json.dumps(payload),
    )

    chunks: list[str] = []
    for event in resp.get("completion", []):
        chunk = event.get("chunk")
        if chunk and "bytes" in chunk:
            chunks.append(chunk["bytes"].decode("utf-8", errors="replace"))
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
