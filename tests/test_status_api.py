"""Tests for the read-only status polling API (FEAT-003 / ADR-003).

Covers orchestrator/handler.py ``status_handler`` (GET /tasks/{task_id}) against
the in-memory fakes:

* an existing in_flight row -> 200 with status + phase;
* a done row -> 200 with pr_url;
* a failed / tests_failed row -> 200 with a REDACTED reason;
* an unknown task_id -> 404;
* missing verified JWT claims -> 401;
* the reason field never contains a raw secret (a seeded GITHUB_TOKEN value is
  masked);
* the endpoint is READ-ONLY (no put_item / update_item mutation);
* claiming a job sets phase="queued" on the row (both ingress paths).

SDK stubs for boto3/bedrock_agentcore are installed by tests/conftest.py before
these imports resolve. The ``handler`` fixture reloads the module against fresh
fakes; ``jobs_table`` is the FakeDynamoTable it reads/writes.
"""

import json

import pytest

from fakes import make_ide_jwt_event  # noqa: E402 - conftest installs stubs first


CLAIMS = {"sub": "dev-123", "email": "dev@customer.com"}
TASK_ID = "ide-abc-123"
TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_abcdef0123456789"


def _status_event(task_id, claims=CLAIMS, *, path_param=True) -> dict:
    """Build a GET /tasks/{task_id} HTTP API event with verified claims."""
    event: dict = {}
    if claims is not None:
        event["requestContext"] = {
            "authorizer": {"jwt": {"claims": dict(claims)}}
        }
    if task_id is not None:
        if path_param:
            event["pathParameters"] = {"task_id": task_id}
        else:
            event["task_id"] = task_id
    return event


def _seed(jobs_table, task_id, **attrs) -> None:
    """Seed a status row directly on the fake table (no mutation under test)."""
    item = {"sonar_hash": task_id}
    item.update(attrs)
    jobs_table.items[task_id] = item


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_status_in_flight_returns_status_and_phase(handler, jobs_table):
    _seed(jobs_table, TASK_ID, status="in_flight", phase="cloning",
          updated_at=1700000000)

    resp = handler.status_handler(_status_event(TASK_ID), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["task_id"] == TASK_ID
    assert body["status"] == "in_flight"
    assert body["phase"] == "cloning"
    assert body["updated_at"] == 1700000000
    # No PR / reason for an in-flight row.
    assert "pr_url" not in body
    assert "reason" not in body


def test_status_done_returns_pr_url(handler, jobs_table):
    _seed(jobs_table, TASK_ID, status="done", phase="done",
          pr_url="https://github.com/o/r/pull/7")

    resp = handler.status_handler(_status_event(TASK_ID), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "done"
    assert body["phase"] == "done"
    assert body["pr_url"] == "https://github.com/o/r/pull/7"


def test_status_tests_failed_returns_reason(handler, jobs_table):
    _seed(jobs_table, TASK_ID, status="tests_failed", phase="tests_failed",
          reason="tests still failing after all fix attempts")

    resp = handler.status_handler(_status_event(TASK_ID), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "tests_failed"
    assert body["reason"] == "tests still failing after all fix attempts"


def test_status_failed_returns_reason(handler, jobs_table):
    _seed(jobs_table, TASK_ID, status="failed", phase="failed",
          reason="clone failed")

    resp = handler.status_handler(_status_event(TASK_ID), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["reason"] == "clone failed"


def test_status_direct_dict_task_id_shape(handler, jobs_table):
    """A direct-dict test event carries task_id at the top level."""
    _seed(jobs_table, TASK_ID, status="in_flight", phase="queued")
    resp = handler.status_handler(
        _status_event(TASK_ID, path_param=False), None
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "in_flight"


def test_status_request_context_path_params_shape(handler, jobs_table):
    """HTTP API can also place pathParameters under requestContext."""
    _seed(jobs_table, TASK_ID, status="done", phase="done",
          pr_url="https://github.com/o/r/pull/1")
    event = {
        "requestContext": {
            "authorizer": {"jwt": {"claims": dict(CLAIMS)}},
            "pathParameters": {"task_id": TASK_ID},
        }
    }
    resp = handler.status_handler(event, None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["pr_url"] == "https://github.com/o/r/pull/1"


# ---------------------------------------------------------------------------
# 404 / 400 / 401
# ---------------------------------------------------------------------------


def test_status_unknown_task_returns_404(handler):
    resp = handler.status_handler(_status_event("does-not-exist"), None)
    assert resp["statusCode"] == 404
    assert json.loads(resp["body"])["error"] == "task not found"


def test_status_missing_task_id_returns_400(handler):
    resp = handler.status_handler(_status_event(None), None)
    assert resp["statusCode"] == 400


def test_status_missing_claims_returns_401(handler, jobs_table):
    _seed(jobs_table, TASK_ID, status="in_flight", phase="queued")
    resp = handler.status_handler(_status_event(TASK_ID, claims=None), None)
    assert resp["statusCode"] == 401


def test_status_empty_claims_returns_401(handler, jobs_table):
    _seed(jobs_table, TASK_ID, status="in_flight", phase="queued")
    resp = handler.status_handler(
        _status_event(TASK_ID, claims={"foo": "bar"}), None
    )
    assert resp["statusCode"] == 401


# ---------------------------------------------------------------------------
# Secret redaction: a reason embedding a token is masked
# ---------------------------------------------------------------------------


def test_status_reason_redacts_secret(handler, jobs_table, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    _seed(jobs_table, TASK_ID, status="failed", phase="failed",
          reason=f"clone failed using {TOKEN_SENTINEL}")

    resp = handler.status_handler(_status_event(TASK_ID), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert TOKEN_SENTINEL not in resp["body"]
    assert TOKEN_SENTINEL not in body["reason"]
    assert handler.REDACTION_MASK in body["reason"]


# ---------------------------------------------------------------------------
# Read-only: no mutation
# ---------------------------------------------------------------------------


def test_status_is_read_only(handler, jobs_table):
    _seed(jobs_table, TASK_ID, status="in_flight", phase="cloning")
    # Drop the seeding call from the recorded calls so we only observe the
    # handler's own table access.
    jobs_table.calls.clear()

    handler.status_handler(_status_event(TASK_ID), None)

    ops = {call["op"] for call in jobs_table.calls}
    assert ops == {"get_item"}
    assert "put_item" not in ops
    assert "update_item" not in ops


# ---------------------------------------------------------------------------
# phase="queued" is set on claim (both ingress paths go through _claim_job)
# ---------------------------------------------------------------------------


def test_claim_sets_phase_queued_ide_path(handler, jobs_table):
    body = {
        "repo_clone_url": "https://github.com/your-org/your-repo.git",
        "description": "Add input validation.",
        "summary": "Validate input",
    }
    event = make_ide_jwt_event(body, CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202
    task_id = json.loads(resp["body"])["task_id"]

    item = jobs_table.get_item(Key={"sonar_hash": task_id})["Item"]
    assert item["phase"] == handler.PHASE_QUEUED


def test_claim_sets_phase_queued_jira_path(handler, jobs_table):
    jira_description = (
        "Clone: https://github.com/your-org/your-repo.git\n"
        "Base branch: master\n"
        "Suggested fix branch: sonarqube-fix/xss\n"
        "File to fix: server.js:135\n"
    )
    event = {
        "issue": {
            "key": "PROJ-15",
            "fields": {
                "summary": "XSS fix",
                "description": jira_description,
                "labels": ["sonarqube", "sonar-hash:abc-123"],
            },
        }
    }
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 202

    item = jobs_table.get_item(Key={"sonar_hash": "abc-123"})["Item"]
    assert item["phase"] == handler.PHASE_QUEUED


# ---------------------------------------------------------------------------
# Phase constants stay in sync between handler and agent
# ---------------------------------------------------------------------------


def test_phase_constants_in_sync_with_agent(handler):
    import agent

    assert handler.PHASE_QUEUED == agent.PHASE_QUEUED == "queued"
    assert handler.PHASE_CLONING == agent.PHASE_CLONING == "cloning"
    assert handler.PHASE_FIXING == agent.PHASE_FIXING == "fixing"
    assert handler.PHASE_TESTING == agent.PHASE_TESTING == "testing"
    assert handler.PHASE_OPENING_PR == agent.PHASE_OPENING_PR == "opening_pr"
    assert handler.PHASE_FOR_STATUS == agent.PHASE_FOR_STATUS
