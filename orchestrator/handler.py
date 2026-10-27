"""Orchestrator Lambda — Jira webhook entry point.

Flow:
  API Gateway (HTTP API) POST /webhook
    -> validate label (sonarqube / SONARQUBE-FIX)
    -> parse description Part 1 header + sonar_hash label
    -> idempotency check / claim via DynamoDB (key = sonar_hash)
    -> build TicketPayload
    -> enqueue TicketPayload (JSON) onto SQS and return 202 immediately
    -> 202 / 400 / 500

A separate SQS-triggered Lambda (``sqs_consumer`` below) drains the queue and
invokes the AgentCore Runtime with the payload (JSON, not a rendered prompt).
The runtime writes the terminal job status back to DynamoDB itself.

Webhook authentication (issue #5)
---------------------------------
Incoming webhooks are authenticated BEFORE any body parsing or job-claim work,
with a constant-time compare (``hmac.compare_digest``) for every comparison.
Two credential modes are supported, in this precedence:

  1. HMAC signature mode: when an ``x-hub-signature-256`` header is present, we
     compute ``hmac-sha256(secret, raw_body_bytes)`` and compare (constant-time)
     against the header value (hex, optional ``sha256=`` prefix).
  2. Shared-secret mode: otherwise, when an ``x-webhook-secret`` (or
     ``x-webhook-token``) header is present, we compare it (constant-time)
     against ``WEBHOOK_SECRET``.

Fail-closed policy keyed on whether ``WEBHOOK_SECRET`` is configured:

  * CONFIGURED (non-empty): a webhook with NO recognised credential header is
    rejected 401 (missing credential); a present-but-invalid signature/secret is
    rejected 403. A valid credential is accepted.
  * UNSET/empty: verification is SKIPPED (a redacted warning is logged) so
    local/direct-invoke and unconfigured deployments keep working. This is the
    path the existing raw-dict-event tests exercise.

The direct-dict invoke path (an event carrying ``issue`` and no ``body``) has no
raw body and no signature, so it is treated as a trusted local invoke and is
allowed regardless of mode. The secret value never reaches a response body, log
line, or exception: ``WEBHOOK_SECRET`` is part of redact()'s coverage.

Repo allowlist (issue #5)
-------------------------
``REPO_ALLOWLIST`` is a comma/whitespace separated list of ``owner/repo`` glob
patterns (e.g. ``your-org/*``). After the description header parses, the
orchestrator rejects (403) any ticket whose repo is not allowed BEFORE claiming
or enqueuing. An empty/unset allowlist means "no allowlist configured" and
allows all repos (opt-in). The runtime re-checks the same allowlist (defence in
depth, like the duplicated URL/branch validators).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from parser import (
    IdeRequestError,
    extract_sonar_hash,
    has_sonarqube_label,
    parse_description_header,
    parse_ide_request,
    repo_allowed,
)

AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "kiro-minions-jobs")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# SQS queue the webhook producer enqueues TicketPayloads onto (FEAT-004 #7).
# The SQS-triggered consumer Lambda (sqs_consumer) drains it and invokes the
# AgentCore Runtime, so the webhook returns 202 without blocking on the invoke.
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")

# How long a claimed (in_flight) job's lease is valid, in seconds. After it
# expires a stale in_flight row becomes claimable again so a crashed/timed-out
# consumer cannot wedge a sonar_hash forever (FEAT-004 #6).
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "3600"))

# Shared secret / HMAC key used to authenticate incoming webhooks (#5). The SAM
# template injects the RESOLVED secret VALUE here via a dynamic reference (never
# hardcoded). When empty/unset, webhook verification is skipped (fail-open only
# for unconfigured deployments / direct invokes); when set, verification is
# fail-closed. Its value is masked everywhere by redact() (see _SECRET_ENV_VARS).
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Comma/whitespace separated owner/repo glob patterns (#5). Empty => allow-all.
# Parsed once at import, like LEASE_SECONDS.
def _parse_allowlist(raw: str) -> list[str]:
    """Split a REPO_ALLOWLIST env string into owner/repo glob patterns."""
    if not raw:
        return []
    return [p for p in raw.replace(",", " ").split() if p]


REPO_ALLOWLIST = _parse_allowlist(os.environ.get("REPO_ALLOWLIST", ""))

# Per-actor dispatch quota (ADR-007 C). Limits how many IDE tasks a single
# developer identity (``actor`` from the verified JWT claims) can dispatch per
# FIXED window (a tumbling window that resets at each boundary — see
# ``_quota_window_start``), so one developer or a buggy extension cannot flood
# the pipeline (scaled parallelism = scaled cost + blast radius). Enforced in
# ``handler_ide`` after a successful idempotency claim and BEFORE any enqueue,
# via a DynamoDB-backed atomic counter (so idempotent retries do not consume a
# slot — review finding #1).
#
#   ACTOR_QUOTA_MAX            — max dispatches per actor per window.
#                                0 / unset => UNLIMITED (quota disabled), which
#                                keeps existing/unconfigured deployments working.
#   ACTOR_QUOTA_WINDOW_SECONDS — the fixed-window length in seconds (default 1h).
#
# The counter is a dedicated item on the SAME JobsTable keyed by the physical
# ``sonar_hash`` attribute as ``quota#<actor>#<window_start>`` with a TTL on
# ``lease_expires_at`` (reused as the table's TTL attribute) so expired windows
# are swept automatically. A counter-backend error is treated as fail-OPEN (the
# dispatch proceeds) so a transient DynamoDB blip cannot wedge the whole ingress
# path; the error is logged redacted.
ACTOR_QUOTA_MAX = int(os.environ.get("ACTOR_QUOTA_MAX", "0"))
ACTOR_QUOTA_WINDOW_SECONDS = int(
    os.environ.get("ACTOR_QUOTA_WINDOW_SECONDS", "3600")
)

# Environment variables whose VALUES must never appear in a response body or log
# line. These are usually NOT present in the orchestrator's environment, so this
# is defence in depth (the runtime is the primary secret holder). redact() reads
# them from os.environ at call time rather than caching at import.
_SECRET_ENV_VARS = ("GITHUB_TOKEN", "KIRO_API_KEY", "WEBHOOK_SECRET")
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

# Job-state values stored in DynamoDB. These string values MUST stay in sync
# with the terminal statuses the runtime writes (runtime/agent.py: STATUS_DONE /
# STATUS_FAILED / STATUS_TESTS_FAILED), otherwise a status the runtime writes
# can become unrecognised here and wedge the sonar_hash.
STATUS_IN_FLIGHT = "in_flight"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
# The runtime writes this when kiro produced no change / the tests never passed.
# It is a non-success terminal state, but a later webhook must still be able to
# re-dispatch it (e.g. after the ticket is adjusted), so it is RETRYABLE: only
# a successful ``done`` permanently blocks re-dispatch.
STATUS_TESTS_FAILED = "tests_failed"

# Statuses that block a re-dispatch (idempotency). Only a successful terminal
# ``done`` (and a live in_flight lease) blocks; ``failed`` and ``tests_failed``
# are retryable.
_BLOCKING_STATUSES = {STATUS_IN_FLIGHT, STATUS_DONE}

# Coarse progress phase (ADR-003) stored alongside ``status`` so the IDE can
# show "queued -> cloning -> fixing -> testing -> opening_pr -> <terminal>".
# These string values MUST stay in sync with the runtime (runtime/agent.py:
# PHASE_* and PHASE_FOR_STATUS); a test asserts equality. The orchestrator sets
# phase="queued" on claim; the runtime emits the intermediate phases and a
# terminal phase mapped from the terminal status it writes.
PHASE_QUEUED = "queued"
PHASE_CLONING = "cloning"
PHASE_FIXING = "fixing"
PHASE_TESTING = "testing"
PHASE_OPENING_PR = "opening_pr"
# Terminal phase mapping aligned to the existing terminal statuses. A terminal
# status is also surfaced as a terminal phase so the IDE sees a single coherent
# progress field.
PHASE_FOR_STATUS = {
    STATUS_DONE: "done",
    STATUS_FAILED: "failed",
    STATUS_TESTS_FAILED: "tests_failed",
}

_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_agentcore = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
_sqs = boto3.client("sqs", region_name=AWS_REGION)


def _response(status_code: int, body: dict) -> dict:
    """Build an API Gateway (proxy) response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# Credential headers (lowercased). HMAC signature takes precedence over the
# plain shared-secret header when both are present.
_SIGNATURE_HEADER = "x-hub-signature-256"
_SECRET_HEADERS = ("x-webhook-secret", "x-webhook-token")


class WebhookAuthError(Exception):
    """Raised when webhook authentication fails.

    ``status_code`` is 401 (no recognised credential header) or 403 (a credential
    was presented but did not verify). The message carries no secret value.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _raw_body_and_headers(event: dict) -> tuple[str | None, dict]:
    """Return the raw request body string and lowercased-key headers.

    The body is returned EXACTLY as received (after base64 decode when
    ``event['isBase64Encoded']`` is set) and BEFORE any JSON parsing, which is
    what HMAC/secret verification needs. For a direct-dict invoke (an event that
    carries ``issue`` and no ``body``) there is no raw body to verify, so the raw
    body is ``None``. Header keys are lowercased (API Gateway HTTP API already
    lowercases them, but we normalise defensively).

    Review finding #5 (KEEP IN SYNC): the base64/body-shape decoding here MUST
    mirror ``_extract_body`` below so the bytes HMAC signs are exactly the bytes
    that later get JSON-parsed. See ``_extract_body`` for the full note.
    """
    if event is None:
        raise ValueError("empty event")

    headers = {
        str(k).lower(): v for k, v in (event.get("headers") or {}).items()
    }

    # Direct invocation already shaped like a Jira webhook body: no raw body.
    if "issue" in event and "body" not in event:
        return None, headers

    body = event.get("body")
    if body is None:
        raise ValueError("event has no 'body'")

    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")

    if isinstance(body, str):
        return body, headers
    # A dict body (some direct-invoke shapes) has no stable raw encoding to sign;
    # treat it as having no raw body for verification purposes.
    if isinstance(body, dict):
        return None, headers
    raise ValueError(f"unsupported body type: {type(body)!r}")


def verify_webhook(raw_body: str | None, headers: dict) -> None:
    """Authenticate an incoming webhook; raise WebhookAuthError on failure.

    No-ops when ``WEBHOOK_SECRET`` is unset/empty (fail-open for unconfigured
    deployments) or when there is no raw body (direct/local invoke). Otherwise
    fails closed: see the module docstring for the precise 401/403 policy. All
    comparisons use ``hmac.compare_digest`` (constant-time).
    """
    secret = WEBHOOK_SECRET
    if not secret:
        # Unconfigured: skip verification but surface a redacted warning so an
        # accidentally-unprotected deployment is visible in the logs.
        print(redact("WEBHOOK_SECRET not configured — skipping webhook verification"))
        return

    # Direct/local invoke (no raw body): nothing to sign, treat as trusted.
    if raw_body is None:
        return

    headers = headers or {}
    secret_bytes = secret.encode("utf-8")

    # 1. HMAC signature mode (preferred when the signature header is present).
    signature = headers.get(_SIGNATURE_HEADER)
    if signature:
        provided = signature.strip()
        if provided.lower().startswith("sha256="):
            provided = provided[len("sha256="):]
        # Review finding #3: hexdigest() is lowercase, so a sender supplying
        # UPPERCASE hex (GitHub always sends lowercase, but non-GitHub senders
        # may not) would be rejected 403. Case-fold the provided hex before the
        # constant-time compare. Hex is case-insensitive, so this does not weaken
        # the check; compare_digest still runs over equal-length lowercase hex.
        provided = provided.lower()
        expected = hmac.new(
            secret_bytes, raw_body.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(provided, expected):
            raise WebhookAuthError(403, "invalid webhook signature")
        return

    # 2. Shared-secret header mode.
    for name in _SECRET_HEADERS:
        provided = headers.get(name)
        if provided is not None:
            if not hmac.compare_digest(provided.encode("utf-8"), secret_bytes):
                raise WebhookAuthError(403, "invalid webhook secret")
            return

    # Configured secret but no credential header at all -> missing credential.
    raise WebhookAuthError(401, "missing webhook signature/secret header")


def _extract_body(event: dict) -> dict:
    """Pull and JSON-parse the request body from an API Gateway event.

    Handles both raw-dict events (direct invoke) and API Gateway proxy events
    where the body is a JSON string (optionally base64-encoded).

    Review finding #5 (KEEP IN SYNC): the base64/body-shape decoding here MUST
    mirror ``_raw_body_and_headers`` above. They are deliberately separate: that
    one returns the raw PRE-JSON bytes HMAC needs, this one returns the PARSED
    dict. Verification signs the exact bytes returned by ``_raw_body_and_headers``
    and this function must not alter those bytes before parsing, or a signed body
    could parse to something the signature never covered. Any change to the
    base64/isBase64Encoded/bytes handling in one of these two functions must be
    made in the other, or the sign-then-parse contract breaks.
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


def _claim_job(sonar_hash: str, ticket_id: str, extra: dict | None = None) -> bool:
    """Atomically claim a job for this logical task_id / sonar_hash.

    ``sonar_hash`` is the LOGICAL task_id (ADR-004): for the Jira path it is the
    SonarQube hash (1:1, unchanged behaviour); for the IDE path it is the
    derived/supplied task_id. Either way it is written into the PHYSICAL DynamoDB
    partition-key attribute ``sonar_hash`` (no migration), so the lease /
    reclaim ConditionExpression and ``_mark_status`` are reused unchanged and the
    runtime (which writes status keyed by ``sonar_hash``) updates the same row.

    ``extra`` carries additional attributes to persist on the claimed row (e.g.
    ``source`` / ``actor`` for an IDE task). It must not override the managed
    bookkeeping fields.

    Returns True if we acquired the claim (new or previously-failed), False if a
    blocking record (in_flight / done) already exists -> caller should skip.
    """
    table = _dynamodb.Table(DYNAMODB_TABLE)
    now = int(time.time())
    lease_expires_at = now + LEASE_SECONDS
    item = {
        "sonar_hash": sonar_hash,
        "ticket_id": ticket_id,
        "status": STATUS_IN_FLIGHT,
        # Coarse progress phase (ADR-003): a freshly claimed job is "queued"
        # until the runtime starts emitting intermediate phase writes. Set for
        # BOTH the Jira and IDE claim paths (both go through _claim_job).
        "phase": PHASE_QUEUED,
        "created_at": now,
        "updated_at": now,
        "lease_expires_at": lease_expires_at,
    }
    if extra:
        for k, v in extra.items():
            item.setdefault(k, v)
    # A claim succeeds when one of these holds:
    #   * the item does not exist yet,
    #   * its status is "failed" (a previous run failed -> retry allowed),
    #   * its status is "tests_failed" (the runtime produced no change / the
    #     tests never passed -> a later webhook may re-dispatch after the ticket
    #     is adjusted; this is a non-success terminal state, not a dead end), or
    #   * its status is "in_flight" AND its lease has expired (#l < :now) -> the
    #     previous consumer crashed/timed out, so the stale lease is reclaimable.
    # A "done" row has no matching clause, so it alone keeps blocking
    # re-dispatch: only a successful terminal PR permanently wedges the hash.
    cond = (
        "attribute_not_exists(sonar_hash)"
        " OR #s = :failed"
        " OR #s = :tests_failed"
        " OR (#s = :in_flight AND #l < :now)"
    )
    try:
        table.put_item(
            Item=item,
            ConditionExpression=cond,
            ExpressionAttributeNames={"#s": "status", "#l": "lease_expires_at"},
            ExpressionAttributeValues={
                ":failed": STATUS_FAILED,
                ":tests_failed": STATUS_TESTS_FAILED,
                ":in_flight": STATUS_IN_FLIGHT,
                ":now": now,
            },
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


# ---------------------------------------------------------------------------
# Audit log of dispatch decisions (ADR-007 E)
#
# Every dispatch decision — in BOTH the Jira ``handler`` and the IDE
# ``handler_ide`` — emits one structured, REDACTED audit record so operators can
# answer "who dispatched what, when, and what happened" for multi-tenant
# accountability and cost/abuse debugging. v1 is a single redacted JSON log line
# via print() (matching the existing redact()/print() logging style); a
# DynamoDB audit item is a deferred follow-up. Every string field is routed
# through redact() so no secret (GITHUB_TOKEN / KIRO_API_KEY / WEBHOOK_SECRET)
# can ever reach the audit output.
# ---------------------------------------------------------------------------

# Claim-outcome values recorded in the audit log.
AUDIT_CLAIMED = "claimed"
AUDIT_SKIPPED = "skipped"
AUDIT_QUOTA_EXCEEDED = "quota_exceeded"
AUDIT_REJECTED = "rejected"


def _audit(outcome: str, *, source: str, actor: str = "", task_id: str = "",
           repo: str = "", reason: str = "") -> None:
    """Emit one redacted, structured audit record for a dispatch decision.

    ``outcome`` is one of the AUDIT_* values. The record carries who (actor),
    what (task_id, repo, source), when (timestamp), the claim outcome, and an
    optional short reason. Best-effort: never raises, so audit logging cannot
    break the request path. All free-text is routed through redact().
    """
    record = {
        "audit": "dispatch_decision",
        "ts": int(time.time()),
        "source": source,
        "actor": redact(actor) or "",
        "task_id": redact(task_id) or "",
        "repo": redact(repo) or "",
        "outcome": outcome,
    }
    if reason:
        record["reason"] = redact(reason)
    try:
        print(json.dumps(record))
    except Exception:  # noqa: BLE001 - audit logging must never break dispatch
        pass


# ---------------------------------------------------------------------------
# Per-actor dispatch quota (ADR-007 C)
# ---------------------------------------------------------------------------


# Structured marker emitted whenever the per-actor quota FAILS OPEN (ADR-007 C,
# review finding #2). It is a distinct, machine-greppable JSON line (not the
# generic redacted print) so a CloudWatch metric filter / alarm can fire on it:
# the quota exists specifically to bound scaled cost/blast radius, so the moment
# it silently stops enforcing is exactly when an operator wants a signal. The
# ``reason`` disambiguates a backend error from an unreadable counter read.
QUOTA_FAIL_OPEN_EVENT = "quota_fail_open"


def _emit_quota_fail_open(actor: str, reason: str) -> None:
    """Emit one structured, greppable, redacted quota fail-open signal.

    Best-effort: never raises (a logging failure must not wedge dispatch). The
    distinct ``event`` key lets a CloudWatch metric filter count these and alarm
    so fail-open is OBSERVABLE, not buried in a generic log line. Carries no
    secret (actor + reason are routed through redact()).
    """
    record = {
        "event": QUOTA_FAIL_OPEN_EVENT,
        "ts": int(time.time()),
        "actor": redact(actor) or "",
        "reason": redact(reason) or "",
    }
    try:
        print(json.dumps(record))
    except Exception:  # noqa: BLE001 - observability must never break dispatch
        pass


def _quota_window_start(now: int) -> int:
    """Return the start timestamp of the FIXED quota window containing ``now``.

    This is a FIXED (tumbling) window, not a rolling/sliding one: ``now //
    window`` buckets time into contiguous fixed windows and the per-actor
    allowance resets hard at each window boundary. (A rolling window would
    instead count dispatches in the trailing ``window`` seconds from ``now``.)
    Fixed is intentional for v1 — it is a single atomic counter per
    actor+window, cheap and race-free; a sliding window would need per-dispatch
    timestamps and a range read.
    """
    window = ACTOR_QUOTA_WINDOW_SECONDS if ACTOR_QUOTA_WINDOW_SECONDS > 0 else 1
    return (now // window) * window


def _check_actor_quota(actor: str) -> bool:
    """Atomically count this dispatch against the actor's quota window.

    Returns True when the dispatch is WITHIN the quota (or the quota is
    disabled), False when the actor has EXCEEDED the limit for the current
    window (caller returns 429 before any enqueue).

    Implementation: an atomic ``UpdateItem ADD`` on a dedicated counter item
    keyed by ``quota#<actor>#<window_start>`` on the physical ``sonar_hash``
    attribute of the JobsTable, with a TTL so expired FIXED windows are swept.
    The ADD returns the post-increment value (ReturnValues=UPDATED_NEW); if it
    exceeds ACTOR_QUOTA_MAX the actor is over the limit. A backend error is
    fail-OPEN (returns True) so a transient DynamoDB blip cannot wedge the
    ingress path; the fail-open is emitted as a distinct, greppable signal
    (``_emit_quota_fail_open``) so an operator can alarm on a sustained fault
    that silently removes the abuse ceiling (finding #2). The signal carries no
    secret (routed through redact()).
    """
    if ACTOR_QUOTA_MAX <= 0:
        # Quota disabled / unlimited.
        return True

    now = int(time.time())
    window_start = _quota_window_start(now)
    counter_key = f"quota#{actor}#{window_start}"
    # TTL a little past the end of the window so DynamoDB sweeps stale counters.
    ttl = window_start + ACTOR_QUOTA_WINDOW_SECONDS + 60

    table = _dynamodb.Table(DYNAMODB_TABLE)
    try:
        resp = table.update_item(
            Key={"sonar_hash": counter_key},
            UpdateExpression="ADD #c :one SET #t = :ttl",
            ExpressionAttributeNames={"#c": "count", "#t": "lease_expires_at"},
            ExpressionAttributeValues={":one": 1, ":ttl": ttl},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as exc:
        # Fail open: a counter-backend error must not wedge the request path,
        # but emit an observable signal so the lost ceiling can be alarmed on.
        _emit_quota_fail_open(actor, f"counter backend error: {exc}")
        return True

    count = (resp or {}).get("Attributes", {}).get("count")
    if count is None:
        # Could not read back the post-increment value -> fail open (observable).
        _emit_quota_fail_open(actor, "counter post-increment value unreadable")
        return True
    return count <= ACTOR_QUOTA_MAX


def _build_payload(ticket_id: str, summary: str, description: str,
                   header: dict, sonar_hash: str) -> dict:
    """Assemble the TicketPayload contract object.

    This payload is sent as-is (JSON) to the AgentCore Runtime. The runtime's
    agent.py receives it directly — no prompt rendering needed.
    """
    ticket_url = (
        f"{JIRA_BASE_URL}/browse/{ticket_id}" if JIRA_BASE_URL and ticket_id else ""
    )
    # TaskPayload superset (ADR-005): additive source/actor; every runtime-read
    # field name is unchanged. The Jira path emits source="jira" (actor empty).
    return {
        "source": "jira",
        "actor": "",
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


def _build_ide_payload(mapped: dict) -> dict:
    """Assemble the TaskPayload (JSON) for an IDE task (source="kiro-ide").

    ``mapped`` comes from ``parse_ide_request``. The derived/supplied logical
    ``task_id`` is carried in the existing ``sonar_hash`` field (ADR-004/005) so
    the runtime writes status back to the same claimed row with NO wire change:
    runtime/agent.py keys ``write_job_status`` on ``sonar_hash`` and falls back
    to ``fix_branch`` derived from ``sonar_hash``. Jira-only fields
    (``ticket_url`` / ``ticket_id``) are empty for an IDE task.
    """
    task_id = mapped["task_id"]
    return {
        "source": "kiro-ide",
        "actor": mapped["actor"],
        "ticket_id": "",
        "summary": mapped["summary"],
        "description": mapped["description"],
        "ticket_url": "",
        "repo_clone_url": mapped["repo_clone_url"],
        "base_branch": mapped["base_branch"],
        "fix_branch": mapped["fix_branch"],
        "file_to_fix": mapped["file_to_fix"],
        "acceptance": mapped["acceptance"],
        # Logical task_id carried in the physical partition-key field so the
        # runtime writes status back to the same row (no runtime wire change).
        "sonar_hash": task_id,
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


def _enqueue_payload(payload: dict) -> None:
    """Send the TicketPayload JSON to the SQS work queue.

    The SQS-triggered consumer Lambda (``sqs_consumer``) later drains the queue
    and performs the blocking AgentCore invoke, so the webhook can return 202
    immediately (#7). Raises if SQS_QUEUE_URL is unset or send_message fails.
    """
    if not SQS_QUEUE_URL:
        raise RuntimeError("SQS_QUEUE_URL env var is not set")
    _sqs.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps(payload),
    )


def sqs_consumer(event, context):  # noqa: ANN001 - Lambda signature
    """SQS-triggered Lambda: drain the queue and invoke the AgentCore Runtime.

    Each record body is a TicketPayload JSON produced by ``handler`` /
    ``_enqueue_payload``. For every record we call ``_invoke_runtime(payload)``,
    which performs the (blocking) AgentCore invoke. On failure we RE-RAISE so
    SQS retries the message and, after the configured attempts, routes it to the
    DLQ. We do NOT record job status here: the runtime writes the terminal
    status to DynamoDB itself (#6), so the consumer stays thin.
    """
    records = (event or {}).get("Records") or []
    for record in records:
        body = record.get("body")
        payload = json.loads(body) if isinstance(body, str) else body
        sonar_hash = (payload or {}).get("sonar_hash")
        try:
            _invoke_runtime(payload)
        except Exception as exc:  # noqa: BLE001 - re-raise after logging redacted
            # Let SQS retry / DLQ. Never let a secret reach the log line.
            print(redact(
                f"runtime invocation failed for sonar_hash={sonar_hash}: {exc}"
            ))
            raise
    return {"status": "ok", "processed": len(records)}


def handler(event, context):  # noqa: ANN001 - Lambda signature
    """Lambda entry point."""
    # --- Authenticate the webhook BEFORE any parsing/claim work (#5) ---
    # Verify against the raw body + headers first so an unauthenticated request
    # never reaches JSON parsing, the label gate, or a job claim (fail closed
    # early). The secret value is never echoed into the response.
    try:
        raw_body, req_headers = _raw_body_and_headers(event)
    except ValueError as exc:
        return _response(400, {"error": redact(f"invalid request body: {exc}")})

    try:
        verify_webhook(raw_body, req_headers)
    except WebhookAuthError as exc:
        return _response(exc.status_code, {"error": redact(str(exc))})

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

    # --- Repo allowlist gate (#5) ---
    # Reject a disallowed repo with 403 BEFORE any claim or enqueue, so it never
    # wedges a sonar_hash or reaches the work queue. owner/repo is not a secret,
    # so it is safe to name in the reason (still routed through redact()).
    if not repo_allowed(header["repo_clone_url"], REPO_ALLOWLIST):
        _audit(AUDIT_REJECTED, source="jira", task_id=sonar_hash,
               repo=header["repo_clone_url"], reason="repo not in allowlist")
        return _response(
            403,
            {"error": redact(
                f"repo not in allowlist: {header['repo_clone_url']}"
            )},
        )

    # --- Idempotency claim ---
    try:
        claimed = _claim_job(sonar_hash, ticket_id)
    except ClientError as exc:
        _audit(AUDIT_REJECTED, source="jira", task_id=sonar_hash,
               repo=header["repo_clone_url"], reason=f"dynamodb error: {exc}")
        return _response(500, {"error": redact(f"dynamodb error: {exc}")})

    if not claimed:
        _audit(AUDIT_SKIPPED, source="jira", task_id=sonar_hash,
               repo=header["repo_clone_url"],
               reason="job already in_flight or done")
        return _response(
            200,
            {
                "status": "skipped",
                "reason": "job already in_flight or done for this sonar_hash",
                "sonar_hash": sonar_hash,
            },
        )

    # --- Build payload and enqueue for the consumer (#7) ---
    payload = _build_payload(ticket_id, summary, description, header, sonar_hash)

    try:
        _enqueue_payload(payload)
    except Exception as exc:  # noqa: BLE001 - report any enqueue failure
        # Enqueue failed before any work started: release the claim by marking
        # the job failed so a later webhook can retry, then report a redacted
        # 500. The reason string is redacted and carries no runtime output.
        _mark_status(sonar_hash, STATUS_FAILED, {"error": redact(str(exc))})
        _audit(AUDIT_REJECTED, source="jira", task_id=sonar_hash,
               repo=header["repo_clone_url"], reason=f"enqueue failed: {exc}")
        return _response(
            500, {"error": redact(f"enqueue failed: {exc}")}
        )

    # The webhook returns 202 the moment the payload is enqueued. It carries no
    # runtime output and no secret — the terminal job result is surfaced via the
    # DynamoDB job status the runtime writes itself, not the HTTP body.
    _audit(AUDIT_CLAIMED, source="jira", task_id=sonar_hash,
           repo=header["repo_clone_url"])
    return _response(
        202,
        {
            "status": "accepted",
            "sonar_hash": sonar_hash,
            "ticket_id": ticket_id,
        },
    )


# ---------------------------------------------------------------------------
# Kiro IDE ingress (FEAT-002)
#
# A second ingress for per-developer IDE tasks (specs/ide-ingress.md). It sits
# behind an API Gateway HTTP API JWT authorizer (ADR-001), so API Gateway has
# already VERIFIED the token before this Lambda runs; we only READ the verified
# claims (sub/email -> actor) and never re-verify the signature. We DO fail
# closed (401) if no verified claims are present. Everything downstream (the
# logical-task_id claim on the existing table, the SQS JobsQueue, the runtime)
# is reused unchanged — only the ingress + payload mapping are new.
# ---------------------------------------------------------------------------


def _extract_jwt_claims(event: dict) -> dict:
    """Return the verified JWT claims from an HTTP API event, lowercased keys.

    Tolerates two shapes:
      * API Gateway HTTP API v2: ``event.requestContext.authorizer.jwt.claims``
        (the authorizer already verified the token, so these claims are trusted).
      * A direct-dict test shape: a top-level ``claims`` mapping.

    Returns an empty dict when no verified claims are present (the caller fails
    closed with 401). Never raises.
    """
    if not isinstance(event, dict):
        return {}

    claims = None
    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        authorizer = request_context.get("authorizer")
        if isinstance(authorizer, dict):
            jwt = authorizer.get("jwt")
            if isinstance(jwt, dict):
                claims = jwt.get("claims")
            # Some authorizer shapes put claims directly under authorizer.
            if claims is None and isinstance(authorizer.get("claims"), dict):
                claims = authorizer.get("claims")

    # Direct-dict test shape.
    if claims is None and isinstance(event.get("claims"), dict):
        claims = event.get("claims")

    if not isinstance(claims, dict):
        return {}
    return {str(k).lower(): v for k, v in claims.items()}


def _actor_from_claims(claims: dict) -> str:
    """Derive the actor identity from verified JWT claims (sub/email).

    Prefers ``sub`` (stable subject), falls back to ``email``. Returns an empty
    string when neither is present (the caller fails closed with 401).
    """
    for key in ("sub", "email"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def handler_ide(event, context):  # noqa: ANN001 - Lambda signature
    """Lambda entry point for the Kiro IDE task ingress (POST /tasks).

    Flow:
      1. Read the verified JWT claims (401 if no actor identity).
      2. Parse + map the IDE request body to TaskPayload fields (400 on invalid
         clone URL / branch / missing required field).
      3. Enforce the repo allowlist (403) BEFORE any claim/enqueue, matching the
         Jira path ordering.
      4. Derive the logical task_id (already done by the parser) and claim it on
         the existing table (200 skipped if blocked — a skipped claim is an
         idempotent retry and does NOT consume quota).
      4b. Only on a SUCCESSFUL (new) claim, count the dispatch against the
         per-actor quota (429 over the limit, releasing the claim). Quota runs
         after the claim so idempotent retries never erode it (finding #1), but
         still strictly before any enqueue of genuinely new work.
      5. Build the TaskPayload (source="kiro-ide", actor set) and enqueue onto
         the existing SQS JobsQueue; on enqueue failure mark failed + redacted
         500.
      6. Return 202 {status: "accepted", task_id, actor}. No secret or raw
         runtime output in the response.
    """
    # --- Authenticate: read verified JWT claims (fail closed) ---
    claims = _extract_jwt_claims(event)
    actor = _actor_from_claims(claims)
    if not actor:
        _audit(AUDIT_REJECTED, source="kiro-ide",
               reason="missing verified developer identity")
        return _response(401, {"error": "missing verified developer identity"})

    # --- Parse the IDE request body ---
    try:
        body = _extract_body(event)
    except (ValueError, json.JSONDecodeError) as exc:
        _audit(AUDIT_REJECTED, source="kiro-ide", actor=actor,
               reason=f"invalid request body: {exc}")
        return _response(400, {"error": redact(f"invalid request body: {exc}")})

    # --- Validate + map the request to TaskPayload fields ---
    try:
        mapped = parse_ide_request(body, actor)
    except (IdeRequestError, ValueError) as exc:
        # IdeRequestError covers missing/invalid request fields; the reused
        # validate_clone_url / validate_branch_name raise plain ValueError on a
        # bad URL / branch. Both are client errors -> redacted 400.
        _audit(AUDIT_REJECTED, source="kiro-ide", actor=actor,
               reason=f"invalid request: {exc}")
        return _response(400, {"error": redact(str(exc))})

    task_id = mapped["task_id"]
    repo_clone_url = mapped["repo_clone_url"]

    # --- Repo allowlist gate (before any claim/enqueue, like the Jira path) ---
    if not repo_allowed(repo_clone_url, REPO_ALLOWLIST):
        _audit(AUDIT_REJECTED, source="kiro-ide", actor=actor, task_id=task_id,
               repo=repo_clone_url, reason="repo not in allowlist")
        return _response(
            403,
            {"error": redact(f"repo not in allowlist: {repo_clone_url}")},
        )

    # --- Idempotency claim (logical task_id -> physical sonar_hash attr) ---
    # The claim runs BEFORE the quota check (review finding #1): an idempotent
    # re-dispatch of an already-claimed/done task_id returns 200 "skipped" with
    # NO new work enqueued, so it must NOT consume a quota slot. Only a
    # SUCCESSFUL claim (genuinely new work) is counted against the quota below,
    # which still gates strictly before any enqueue.
    try:
        claimed = _claim_job(
            task_id, task_id, extra={"source": "kiro-ide", "actor": actor}
        )
    except ClientError as exc:
        _audit(AUDIT_REJECTED, source="kiro-ide", actor=actor, task_id=task_id,
               repo=repo_clone_url, reason=f"dynamodb error: {exc}")
        return _response(500, {"error": redact(f"dynamodb error: {exc}")})

    if not claimed:
        # Skipped claim: no new work, no quota consumed. This is the idempotent
        # retry path (double-click / dropped 202) — returning 429 here would
        # wrongly block a user from retrying the SAME task they already queued.
        _audit(AUDIT_SKIPPED, source="kiro-ide", actor=actor, task_id=task_id,
               repo=repo_clone_url,
               reason="task already in_flight or done")
        return _response(
            200,
            {
                "status": "skipped",
                "reason": "task already in_flight or done for this task_id",
                "task_id": task_id,
            },
        )

    # --- Per-actor dispatch quota (ADR-007 C), AFTER a successful claim and
    # BEFORE any enqueue ---
    # Count this NEW dispatch against the actor's fixed (tumbling) window. Over
    # the limit -> 429 with a redacted reason and NO enqueue. Because this runs only after
    # a fresh claim, idempotent retries of an existing task_id never erode the
    # quota (finding #1). Over the limit we release the just-made claim (mark it
    # failed) so the task_id stays retryable rather than wedged in_flight. The
    # quota is disabled when ACTOR_QUOTA_MAX is 0/unset.
    if not _check_actor_quota(actor):
        _mark_status(task_id, STATUS_FAILED,
                     {"reason": redact("per-actor dispatch quota exceeded")})
        _audit(AUDIT_QUOTA_EXCEEDED, source="kiro-ide", actor=actor,
               task_id=task_id, repo=repo_clone_url,
               reason="per-actor dispatch quota exceeded")
        return _response(
            429,
            {"error": redact("per-actor dispatch quota exceeded"),
             "task_id": task_id},
        )

    # --- Build the TaskPayload and enqueue for the consumer ---
    payload = _build_ide_payload(mapped)

    try:
        _enqueue_payload(payload)
    except Exception as exc:  # noqa: BLE001 - report any enqueue failure
        # Enqueue failed before any work started: release the claim by marking
        # the task failed so a later dispatch can retry, then report a redacted
        # 500. The reason string is redacted and carries no runtime output.
        _mark_status(task_id, STATUS_FAILED, {"error": redact(str(exc))})
        _audit(AUDIT_REJECTED, source="kiro-ide", actor=actor, task_id=task_id,
               repo=repo_clone_url, reason=f"enqueue failed: {exc}")
        return _response(500, {"error": redact(f"enqueue failed: {exc}")})

    _audit(AUDIT_CLAIMED, source="kiro-ide", actor=actor, task_id=task_id,
           repo=repo_clone_url)
    return _response(
        202,
        {
            "status": "accepted",
            "task_id": task_id,
            "actor": actor,
        },
    )


# ---------------------------------------------------------------------------
# Status polling API (FEAT-003 / ADR-003)
#
# A READ-ONLY Lambda serving GET /tasks/{task_id}. The IDE polls it every few
# seconds until a terminal status. It sits behind the SAME JWT authorizer as the
# ingress (ADR-001): API Gateway has already verified the token, so we only READ
# the verified claims and fail closed (401) when no identity is present. This
# endpoint performs NO claim / enqueue / mutation — it only reads the existing
# status row the runtime (and _claim_job) write.
#
# The logical ``task_id`` path parameter is read against the PHYSICAL partition
# key attribute ``sonar_hash`` (ADR-004: task_id is the logical alias, the
# attribute name never changed). Any free-text ``reason`` is routed through
# redact() so a secret can never leak back to the IDE.
# ---------------------------------------------------------------------------


def _extract_task_id(event: dict) -> str:
    """Return the ``task_id`` path parameter from an HTTP API event.

    Tolerates the API Gateway HTTP API v2 shape (``event.pathParameters`` and
    ``event.requestContext.pathParameters``) and a direct-dict test shape
    (a top-level ``task_id``). Returns an empty string when absent. Never raises.
    """
    if not isinstance(event, dict):
        return ""

    # Direct-dict test shape.
    direct = event.get("task_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    for container in (event.get("pathParameters"),):
        if isinstance(container, dict):
            value = container.get("task_id")
            if isinstance(value, str) and value.strip():
                return value.strip()

    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        path_params = request_context.get("pathParameters")
        if isinstance(path_params, dict):
            value = path_params.get("task_id")
            if isinstance(value, str) and value.strip():
                return value.strip()

    return ""


def status_handler(event, context):  # noqa: ANN001 - Lambda signature
    """Lambda entry point for GET /tasks/{task_id} (read-only status poll).

    Flow:
      1. Require a verified JWT identity (401 if absent), reusing the FEAT-002
         claims helper.
      2. Read the ``task_id`` path parameter (400 if missing).
      3. Read the status row from DynamoDB by the logical task_id (physical
         ``sonar_hash`` attribute). 404 when the row does not exist.
      4. Return a stable JSON contract: {task_id, status, phase?, pr_url? (when
         done), reason? (REDACTED, when failed/tests_failed), updated_at?}.

    No mutation of any kind happens here.
    """
    # --- Authenticate: read verified JWT claims (fail closed) ---
    claims = _extract_jwt_claims(event)
    actor = _actor_from_claims(claims)
    if not actor:
        return _response(401, {"error": "missing verified developer identity"})

    task_id = _extract_task_id(event)
    if not task_id:
        return _response(400, {"error": "missing task_id path parameter"})

    # --- Read the status row (read-only) ---
    try:
        table = _dynamodb.Table(DYNAMODB_TABLE)
        result = table.get_item(Key={"sonar_hash": task_id})
    except ClientError as exc:
        return _response(500, {"error": redact(f"dynamodb error: {exc}")})

    item = (result or {}).get("Item")
    if not item:
        return _response(404, {"error": "task not found", "task_id": task_id})

    # --- Per-actor ownership scoping for IDE-sourced tasks (finding #3) ---
    # An IDE task row records the dispatching ``actor``; a verified identity may
    # only read its OWN IDE tasks. This closes the "any verified identity can
    # read any task_id" gap without relying solely on task_ids being
    # unguessable. We return 404 (not 403) so the endpoint does not confirm the
    # existence of another tenant's task_id. Jira-sourced rows (source != kiro-
    # ide, actor empty) stay readable by any verified identity — the Jira path
    # has no per-developer owner, so this preserves its existing behaviour.
    if item.get("source") == "kiro-ide":
        owner = item.get("actor") or ""
        if owner and owner != actor:
            return _response(
                404, {"error": "task not found", "task_id": task_id}
            )

    status = item.get("status", "")
    body: dict = {"task_id": task_id, "status": status}

    phase = item.get("phase")
    if phase:
        body["phase"] = phase
    updated_at = item.get("updated_at")
    if updated_at is not None:
        body["updated_at"] = updated_at

    # Surface the PR link only once the job is done.
    if status == STATUS_DONE:
        pr_url = item.get("pr_url")
        if pr_url:
            body["pr_url"] = pr_url

    # Surface a redacted reason for non-success terminal states.
    if status in (STATUS_FAILED, STATUS_TESTS_FAILED):
        reason = item.get("reason")
        if reason is not None:
            # The runtime already redacts, but redact again here as defence in
            # depth so a secret can never leak back to the IDE.
            body["reason"] = redact(reason)

    return _response(200, body)
