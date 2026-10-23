"""Orchestrator Lambda — Jira webhook entry point.

Flow:
  API Gateway (HTTP API) POST /webhook
    -> validate label (sonarqube / SONARQUBE-FIX)
    -> parse description Part 1 header + sonar_hash label
    -> idempotency check / claim via DynamoDB (key = sonar_hash)
    -> build TicketPayload
    -> invoke AgentCore Runtime with payload (JSON, not a rendered prompt)
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

AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "kiro-minions-jobs")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Environment variables whose VALUES must never appear in a response body or log
# line. These are usually NOT present in the orchestrator's environment, so this
# is defence in depth (the runtime is the primary secret holder). redact() reads
# them from os.environ at call time rather than caching at import.
_SECRET_ENV_VARS = ("GITHUB_TOKEN", "KIRO_API_KEY")
REDACTION_MASK = "***REDACTED***"


def redact(text):  # noqa: ANN001 - accepts str | None
    """Mask secret env values (GITHUB_TOKEN, KIRO_API_KEY) in a string.

    Skips empty/unset env values (never masks the empty string) and returns
    ``None`` unchanged so optional error strings pass through safely.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if value:
            text = text.replace(value, REDACTION_MASK)
    return text

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
    """Assemble the TicketPayload contract object.

    This payload is sent as-is (JSON) to the AgentCore Runtime. The runtime's
    agent.py receives it directly — no prompt rendering needed.
    """
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


def _invoke_runtime(payload: dict) -> str:
    """Invoke the AgentCore Runtime with the TicketPayload.

    The Runtime container receives the payload as JSON and runs the
    deterministic fix pipeline (clone → kiro-cli → test → PR). No model
    reasoning happens here — the only intelligence is inside kiro-cli.

    Returns the aggregated response text for logging.
    """
    if not AGENTCORE_RUNTIME_ARN:
        raise RuntimeError("AGENTCORE_RUNTIME_ARN env var is not set")

    # runtimeSessionId must be >= 33 chars.
    session_id = f"kiro-minions-{payload['sonar_hash']}-{uuid.uuid4().hex}"

    resp = _agentcore.invoke(
        runtimeArn=AGENTCORE_RUNTIME_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode("utf-8"),
    )

    # The response may be streaming or a direct body.
    # Handle both: streaming chunks or a single response body.
    chunks: list[str] = []

    if "body" in resp:
        body = resp["body"]
        if hasattr(body, "read"):
            chunks.append(body.read().decode("utf-8", errors="replace"))
        elif isinstance(body, (bytes, bytearray)):
            chunks.append(body.decode("utf-8", errors="replace"))
        elif isinstance(body, str):
            chunks.append(body)

    if "stream" in resp:
        for event in resp["stream"]:
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
        return _response(400, {"error": redact(f"invalid request body: {exc}")})

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
        return _response(400, {"error": redact(str(exc))})

    # --- Idempotency claim ---
    try:
        claimed = _claim_job(sonar_hash, ticket_id)
    except ClientError as exc:
        return _response(500, {"error": redact(f"dynamodb error: {exc}")})

    if not claimed:
        return _response(
            200,
            {
                "status": "skipped",
                "reason": "job already in_flight or done for this sonar_hash",
                "sonar_hash": sonar_hash,
            },
        )

    # --- Build payload and dispatch to the Runtime ---
    payload = _build_payload(ticket_id, summary, description, header, sonar_hash)

    try:
        _invoke_runtime(payload)
    except Exception as exc:  # noqa: BLE001 - report any dispatch failure
        _mark_status(sonar_hash, STATUS_FAILED, {"error": redact(str(exc))})
        return _response(
            500, {"error": redact(f"runtime invocation failed: {exc}")}
        )

    # NOTE: the raw runtime stream text is intentionally NOT returned to the
    # caller — it can echo command output or error text that may carry secrets
    # (issue #3). The dispatch result is surfaced via DynamoDB job status, not
    # the HTTP body. (FEAT-004 removes this synchronous invoke entirely.)
    return _response(
        200,
        {
            "status": "dispatched",
            "sonar_hash": sonar_hash,
            "ticket_id": ticket_id,
        },
    )
