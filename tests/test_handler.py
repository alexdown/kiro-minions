"""Characterization tests for the orchestrator Lambda handler.

These assert what the code does TODAY on the happy path and the obvious
documented branches. No real AWS/GitHub/Jira calls: DynamoDB and SQS are backed
by the in-memory fakes installed by conftest's SDK stubs. No bugs are fixed
here.

NOTE (step-2 async flow, #7): the webhook no longer invokes the AgentCore
Runtime synchronously. It claims the job in DynamoDB, enqueues the TicketPayload
JSON onto SQS, and returns 202 ``accepted`` immediately. A separate SQS-triggered
consumer drains the queue and performs the blocking invoke, and the runtime
writes the terminal status back. These tests therefore assert on the enqueued
SQS message rather than on a synchronous runtime invoke.
"""

from __future__ import annotations

import base64
import json

# The exact PROJ-15/SW15 content, mirrored from tests/test_parser.py.
SW15_DESCRIPTION = """\
Repo URL: https://github.com/your-org/your-repo
Clone: https://github.com/your-org/your-repo.git
Base branch: master
File to fix: server.js:135
Suggested fix branch: sonarqube-fix/xss-swig-autoescape

SONAR-MCP-001: XSS — Template Auto-Escaping Disabled
Priority: P0 (Critical) · Type: Security Vulnerability · CWE-79 · OWASP A03:2021
File: your-repo/server.js · Line: 135 · Rule: javascript:S5247
Hotspot Key: 6f50f92a-65f2-4c4c-aae5-9b069b44430a

Description / Impact / Recommended Fix:
  swig.setDefaults({ autoescape: true });

References · _sonar-hash: 6f50f92a-65f2-4c4c-aae5-9b069b44430a (dedup marker)
"""

SW15_LABELS = [
    "CWE-79",
    "OWASP-A03",
    "P0",
    "SONARQUBE-FIX",
    "XSS",
    "security",
    "sonar-hash:6f50f92a-65f2-4c4c-aae5-9b069b44430a",
    "sonarqube",
]

SW15_HASH = "6f50f92a-65f2-4c4c-aae5-9b069b44430a"


def _webhook_body() -> dict:
    """A valid Jira-webhook-shaped body for the SW15 ticket."""
    return {
        "issue": {
            "key": "PROJ-15",
            "fields": {
                "summary": "Fix XSS in swig autoescape",
                "description": SW15_DESCRIPTION,
                "labels": SW15_LABELS,
            },
        }
    }


def _apigw_event(body: dict, *, base64_encode: bool = False) -> dict:
    """Wrap a body as an API Gateway proxy event."""
    raw = json.dumps(body)
    if base64_encode:
        return {
            "isBase64Encoded": True,
            "body": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
        }
    return {"isBase64Encoded": False, "body": raw}


def _enqueued_payloads(handler) -> list[dict]:
    """Decode every TicketPayload JSON the handler enqueued onto the SQS fake."""
    return [json.loads(m["MessageBody"]) for m in handler._sqs.messages]


def test_happy_path_dispatch(handler, jobs_table):
    event = _apigw_event(_webhook_body())

    resp = handler.handler(event, None)

    # Async flow: the webhook accepts and enqueues, returning 202.
    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["status"] == "accepted"
    assert body["sonar_hash"] == SW15_HASH
    assert body["ticket_id"] == "PROJ-15"

    # In-flight record written to DynamoDB with a lease.
    item = jobs_table.get_item(Key={"sonar_hash": SW15_HASH})["Item"]
    assert item["status"] == "in_flight"
    assert item["ticket_id"] == "PROJ-15"
    assert "lease_expires_at" in item

    # Exactly one TicketPayload enqueued onto SQS, carrying the parsed header.
    payloads = _enqueued_payloads(handler)
    assert len(payloads) == 1
    sent = payloads[0]
    assert sent["sonar_hash"] == SW15_HASH
    assert sent["ticket_id"] == "PROJ-15"
    assert sent["repo_clone_url"] == "https://github.com/your-org/your-repo.git"
    assert sent["base_branch"] == "master"
    assert sent["fix_branch"] == "sonarqube-fix/xss-swig-autoescape"
    assert sent["file_to_fix"] == "server.js:135"


def test_idempotent_skip_second_call(handler):
    event = _apigw_event(_webhook_body())

    first = handler.handler(event, None)
    assert first["statusCode"] == 202
    assert json.loads(first["body"])["status"] == "accepted"

    second = handler.handler(event, None)
    assert second["statusCode"] == 200
    assert json.loads(second["body"])["status"] == "skipped"

    # Only the first call enqueued work.
    assert len(_enqueued_payloads(handler)) == 1


def test_base64_encoded_body_is_decoded_and_dispatched(handler):
    event = _apigw_event(_webhook_body(), base64_encode=True)

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["status"] == "accepted"
    assert len(_enqueued_payloads(handler)) == 1


def test_raw_dict_event_accepted(handler):
    # Direct-invoke event already shaped like a Jira webhook body.
    resp = handler.handler(_webhook_body(), None)

    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["status"] == "accepted"
    assert len(_enqueued_payloads(handler)) == 1
