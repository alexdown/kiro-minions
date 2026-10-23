"""Characterization tests for the orchestrator Lambda handler.

These assert what the code does TODAY on the happy path and the obvious
documented branches. No real AWS/GitHub/Jira calls: DynamoDB is mocked with
moto and the AgentCore Runtime invoke is stubbed. No bugs are fixed here.
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


def test_happy_path_dispatch(handler, agentcore_stub, jobs_table):
    event = _apigw_event(_webhook_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "dispatched"
    assert body["sonar_hash"] == SW15_HASH
    assert body["ticket_id"] == "PROJ-15"

    # In-flight record written to (mocked) DynamoDB.
    item = jobs_table.get_item(Key={"sonar_hash": SW15_HASH})["Item"]
    assert item["status"] == "in_flight"
    assert item["ticket_id"] == "PROJ-15"

    # Runtime invoked exactly once, with a long session id and a JSON payload.
    assert len(agentcore_stub.calls) == 1
    call = agentcore_stub.calls[0]
    assert len(call["runtimeSessionId"]) >= 33
    sent = json.loads(call["payload"].decode("utf-8"))
    assert sent["sonar_hash"] == SW15_HASH
    assert sent["ticket_id"] == "PROJ-15"
    assert sent["repo_clone_url"] == "https://github.com/your-org/your-repo.git"
    assert sent["base_branch"] == "master"
    assert sent["fix_branch"] == "sonarqube-fix/xss-swig-autoescape"
    assert sent["file_to_fix"] == "server.js:135"


def test_idempotent_skip_second_call(handler, agentcore_stub):
    event = _apigw_event(_webhook_body())

    first = handler.handler(event, None)
    assert json.loads(first["body"])["status"] == "dispatched"

    second = handler.handler(event, None)
    assert second["statusCode"] == 200
    assert json.loads(second["body"])["status"] == "skipped"

    # Runtime not invoked a second time.
    assert len(agentcore_stub.calls) == 1


def test_base64_encoded_body_is_decoded_and_dispatched(handler, agentcore_stub):
    event = _apigw_event(_webhook_body(), base64_encode=True)

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "dispatched"
    assert len(agentcore_stub.calls) == 1


def test_raw_dict_event_accepted(handler, agentcore_stub):
    # Direct-invoke event already shaped like a Jira webhook body.
    resp = handler.handler(_webhook_body(), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "dispatched"
    assert len(agentcore_stub.calls) == 1
