"""Tests for FEAT-004 Group B — async SQS dispatch, consumer, and lease.

Covers orchestrator/handler.py:

* The webhook enqueues the TicketPayload onto SQS and returns 202 immediately
  (no runtime output, no secret).
* An enqueue failure releases the claim (marks the job failed) and returns a
  redacted 500.
* ``sqs_consumer`` parses a record body into a payload and invokes the runtime
  once; a runtime failure re-raises so SQS can retry / DLQ.
* ``_claim_job`` stores ``lease_expires_at`` and its ConditionExpression allows
  reclaiming stale in_flight rows (expired lease) while still blocking ``done``.

SDK stubs for boto3/bedrock_agentcore are installed by tests/conftest.py before
these imports resolve.
"""

import json
import time

import pytest

import handler  # noqa: E402 - conftest installs SDK stubs first
from fakes import FakeAgentCoreClient, FakeDynamoResource, FakeSqsClient  # noqa: E402


VALID_DESCRIPTION = (
    "Clone: https://github.com/your-org/your-repo.git\n"
    "Base branch: master\n"
    "Suggested fix branch: sonarqube-fix/xss-swig-autoescape\n"
    "File to fix: server.js:135\n"
)

SONAR_HASH = "6f50f92a-65f2-4c4c-aae5-9b069b44430a"


def _webhook_event(description: str = VALID_DESCRIPTION) -> dict:
    return {
        "issue": {
            "key": "PROJ-15",
            "fields": {
                "summary": "XSS fix",
                "description": description,
                "labels": ["sonarqube", f"sonar-hash:{SONAR_HASH}"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Webhook enqueue -> 202
# ---------------------------------------------------------------------------


def test_handler_enqueues_payload_and_returns_202(monkeypatch):
    sqs = FakeSqsClient()
    monkeypatch.setattr(handler, "_claim_job", lambda h, t: True)
    monkeypatch.setattr(handler, "SQS_QUEUE_URL", "https://sqs.fake/q")
    monkeypatch.setattr(handler, "_sqs", sqs)

    resp = handler.handler(_webhook_event(), None)

    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body == {
        "status": "accepted",
        "sonar_hash": SONAR_HASH,
        "ticket_id": "PROJ-15",
    }
    # No raw runtime output in the body.
    assert "runtime_response" not in body

    # Exactly one send_message recording the expected TicketPayload.
    assert len(sqs.messages) == 1
    sent = sqs.messages[0]
    assert sent["QueueUrl"] == "https://sqs.fake/q"
    payload = json.loads(sent["MessageBody"])
    assert payload["sonar_hash"] == SONAR_HASH
    assert payload["ticket_id"] == "PROJ-15"
    assert payload["repo_clone_url"] == "https://github.com/your-org/your-repo.git"
    assert payload["base_branch"] == "master"
    assert payload["fix_branch"] == "sonarqube-fix/xss-swig-autoescape"


def test_handler_skips_when_claim_blocked(monkeypatch):
    sqs = FakeSqsClient()
    monkeypatch.setattr(handler, "_claim_job", lambda h, t: False)
    monkeypatch.setattr(handler, "SQS_QUEUE_URL", "https://sqs.fake/q")
    monkeypatch.setattr(handler, "_sqs", sqs)

    resp = handler.handler(_webhook_event(), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "skipped"
    # Nothing enqueued when the claim is blocked.
    assert sqs.messages == []


def test_handler_enqueue_failure_marks_job_failed_and_500(monkeypatch):
    marks: list = []
    monkeypatch.setattr(handler, "_claim_job", lambda h, t: True)
    monkeypatch.setattr(handler, "SQS_QUEUE_URL", "https://sqs.fake/q")
    monkeypatch.setattr(
        handler, "_mark_status",
        lambda sonar_hash, status, extra=None: marks.append((sonar_hash, status, extra)),
    )

    sqs = FakeSqsClient()

    def boom(**kwargs):
        raise RuntimeError("sqs unavailable")

    sqs.send_message = boom
    monkeypatch.setattr(handler, "_sqs", sqs)

    resp = handler.handler(_webhook_event(), None)

    assert resp["statusCode"] == 500
    # The job was marked failed so a later webhook can retry.
    assert marks and marks[0][0] == SONAR_HASH
    assert marks[0][1] == handler.STATUS_FAILED


# ---------------------------------------------------------------------------
# sqs_consumer
# ---------------------------------------------------------------------------


def test_sqs_consumer_invokes_runtime_per_record(monkeypatch):
    seen: list = []
    monkeypatch.setattr(handler, "_invoke_runtime", lambda p: seen.append(p))

    payload = {"sonar_hash": SONAR_HASH, "ticket_id": "PROJ-15"}
    event = {"Records": [{"body": json.dumps(payload)}]}

    result = handler.sqs_consumer(event, None)

    assert result["processed"] == 1
    assert len(seen) == 1
    assert seen[0]["sonar_hash"] == SONAR_HASH


def test_sqs_consumer_reraises_on_invoke_failure(monkeypatch):
    def boom(payload):
        raise RuntimeError("invoke blew up")

    monkeypatch.setattr(handler, "_invoke_runtime", boom)

    event = {"Records": [{"body": json.dumps({"sonar_hash": SONAR_HASH})}]}

    with pytest.raises(RuntimeError):
        handler.sqs_consumer(event, None)


# ---------------------------------------------------------------------------
# _claim_job lease matrix (driven through FakeDynamoTable condition logic)
# ---------------------------------------------------------------------------


def _use_fresh_table(monkeypatch) -> FakeDynamoResource:
    resource = FakeDynamoResource()
    monkeypatch.setattr(handler, "_dynamodb", resource)
    return resource


def test_claim_fresh_hash_succeeds_and_stores_lease(monkeypatch):
    resource = _use_fresh_table(monkeypatch)

    assert handler._claim_job(SONAR_HASH, "PROJ-15") is True

    item = resource.Table(handler.DYNAMODB_TABLE).items[SONAR_HASH]
    assert item["status"] == handler.STATUS_IN_FLIGHT
    assert "lease_expires_at" in item
    assert item["lease_expires_at"] > int(time.time())


def test_claim_blocked_when_in_flight_lease_in_future(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    table = resource.Table(handler.DYNAMODB_TABLE)
    # A live in_flight lease in the FUTURE -> not reclaimable.
    table.items[SONAR_HASH] = {
        "sonar_hash": SONAR_HASH,
        "status": handler.STATUS_IN_FLIGHT,
        "lease_expires_at": int(time.time()) + 10_000,
    }

    assert handler._claim_job(SONAR_HASH, "PROJ-15") is False
    # The live lease value was not overwritten.
    assert table.items[SONAR_HASH]["lease_expires_at"] > int(time.time()) + 1_000


def test_claim_reclaims_when_in_flight_lease_expired(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    table = resource.Table(handler.DYNAMODB_TABLE)
    # A stale in_flight lease in the PAST -> reclaimable (crashed consumer).
    table.items[SONAR_HASH] = {
        "sonar_hash": SONAR_HASH,
        "status": handler.STATUS_IN_FLIGHT,
        "lease_expires_at": int(time.time()) - 10,
    }

    assert handler._claim_job(SONAR_HASH, "PROJ-15") is True
    # A fresh lease was written.
    assert table.items[SONAR_HASH]["lease_expires_at"] > int(time.time())


def test_claim_blocked_when_done(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    table = resource.Table(handler.DYNAMODB_TABLE)
    table.items[SONAR_HASH] = {
        "sonar_hash": SONAR_HASH,
        "status": handler.STATUS_DONE,
        "lease_expires_at": int(time.time()) - 10,  # even an old lease stays blocked
    }

    assert handler._claim_job(SONAR_HASH, "PROJ-15") is False
    assert table.items[SONAR_HASH]["status"] == handler.STATUS_DONE


def test_claim_reclaims_when_failed(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    table = resource.Table(handler.DYNAMODB_TABLE)
    table.items[SONAR_HASH] = {
        "sonar_hash": SONAR_HASH,
        "status": handler.STATUS_FAILED,
        "lease_expires_at": int(time.time()) + 10_000,  # future, but failed -> retry
    }

    assert handler._claim_job(SONAR_HASH, "PROJ-15") is True
    assert table.items[SONAR_HASH]["status"] == handler.STATUS_IN_FLIGHT


def test_claim_reclaims_when_tests_failed(monkeypatch):
    # Issue #6 (review v1): the runtime writes ``tests_failed`` on the
    # no-change / tests-never-passed path. That is a non-success terminal state
    # but a later webhook must be able to re-dispatch it: it must NOT wedge the
    # sonar_hash the way a successful ``done`` does. A seeded ``tests_failed``
    # row with a live (future) lease must still be reclaimable.
    resource = _use_fresh_table(monkeypatch)
    table = resource.Table(handler.DYNAMODB_TABLE)
    table.items[SONAR_HASH] = {
        "sonar_hash": SONAR_HASH,
        "status": handler.STATUS_TESTS_FAILED,
        # future lease: reclaim must hinge on the status, not an expired lease.
        "lease_expires_at": int(time.time()) + 10_000,
    }

    assert handler._claim_job(SONAR_HASH, "PROJ-15") is True
    # The claim overwrote it back to in_flight with a fresh lease.
    assert table.items[SONAR_HASH]["status"] == handler.STATUS_IN_FLIGHT
    assert table.items[SONAR_HASH]["lease_expires_at"] > int(time.time())


def test_tests_failed_string_matches_runtime_constant():
    # The reclaimable string must match exactly what runtime/agent.py writes,
    # otherwise a tests_failed row the runtime records would be unrecognised
    # here and wedge the hash. conftest puts runtime/ on sys.path.
    import agent  # noqa: E402 - conftest installs SDK stubs + sys.path first

    assert agent.STATUS_TESTS_FAILED == handler.STATUS_TESTS_FAILED == "tests_failed"
    assert agent.STATUS_DONE == handler.STATUS_DONE == "done"
    assert agent.STATUS_FAILED == handler.STATUS_FAILED == "failed"
