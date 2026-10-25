"""#16 test-gap fill for the orchestrator Lambda (handler.py).

These close genuinely-uncovered branches NOT already exercised by
test_handler.py / test_handler_async.py / test_webhook_auth.py /
test_repo_allowlist.py / test_secret_redaction.py:

* the label gate (missing sonarqube label -> 200 ignored);
* the missing sonar-hash label -> 400;
* a malformed description header -> 400 (redacted);
* a non-conditional DynamoDB ClientError from _claim_job -> 500 (redacted);
* _mark_status swallows a ClientError (best-effort bookkeeping);
* _invoke_runtime aggregating a readable body, a bytes body, and a streaming
  response, and raising when the runtime ARN is unset;
* sqs_consumer accepting a dict record body (not just a JSON string) and a
  no-Records event.

Everything uses the in-memory fakes / stubs installed by conftest; no real AWS
and no moto/boto3 import on the runnable path.
"""

from __future__ import annotations

import json

import pytest

import handler  # noqa: E402 - conftest installs SDK stubs first
from fakes import ClientError, FakeAgentCoreClient  # noqa: E402


SONAR_HASH = "6f50f92a-65f2-4c4c-aae5-9b069b44430a"
VALID_DESCRIPTION = (
    "Clone: https://github.com/your-org/your-repo.git\n"
    "Base branch: master\n"
    "Suggested fix branch: sonarqube-fix/xss-swig-autoescape\n"
    "File to fix: server.js:135\n"
)


def _event(*, labels, description=VALID_DESCRIPTION) -> dict:
    return {
        "issue": {
            "key": "PROJ-15",
            "fields": {
                "summary": "XSS fix",
                "description": description,
                "labels": labels,
            },
        }
    }


# ---------------------------------------------------------------------------
# Label / hash / header gates
# ---------------------------------------------------------------------------


def test_missing_sonarqube_label_is_ignored(handler):
    resp = handler.handler(_event(labels=["bug", f"sonar-hash:{SONAR_HASH}"]), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "ignored"
    # Nothing claimed or enqueued for an unlabelled ticket.
    assert handler._sqs.messages == []


def test_missing_sonar_hash_label_is_400(handler):
    # Has the sonarqube label but no sonar-hash:<key> label.
    resp = handler.handler(_event(labels=["sonarqube"]), None)

    assert resp["statusCode"] == 400
    assert "sonar-hash" in json.loads(resp["body"])["error"]
    assert handler._sqs.messages == []


def test_malformed_description_header_is_400(handler):
    # Missing the Clone:/Base branch:/... header -> parse_description_header
    # raises ValueError -> redacted 400.
    resp = handler.handler(
        _event(labels=["sonarqube", f"sonar-hash:{SONAR_HASH}"],
               description="no header here"),
        None,
    )

    assert resp["statusCode"] == 400
    assert "error" in json.loads(resp["body"])
    assert handler._sqs.messages == []


# ---------------------------------------------------------------------------
# DynamoDB error path on claim -> 500
# ---------------------------------------------------------------------------


def test_claim_dynamodb_error_returns_500(handler, monkeypatch):
    def boom(sonar_hash, ticket_id):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "slow down"}},
            operation_name="PutItem",
        )

    monkeypatch.setattr(handler, "_claim_job", boom)

    resp = handler.handler(
        _event(labels=["sonarqube", f"sonar-hash:{SONAR_HASH}"]), None)

    assert resp["statusCode"] == 500
    assert "dynamodb error" in json.loads(resp["body"])["error"]
    assert handler._sqs.messages == []


def test_claim_non_conditional_clienterror_propagates(handler, monkeypatch):
    """_claim_job re-raises a non-ConditionalCheckFailed ClientError (not
    swallowed as a 'blocked' claim)."""
    table = handler._dynamodb.Table(handler.DYNAMODB_TABLE)

    def boom(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}},
            operation_name="PutItem",
        )

    monkeypatch.setattr(table, "put_item", boom)

    with pytest.raises(ClientError):
        handler._claim_job(SONAR_HASH, "PROJ-15")


# ---------------------------------------------------------------------------
# _mark_status best-effort swallow
# ---------------------------------------------------------------------------


def test_mark_status_swallows_clienterror(handler, monkeypatch):
    table = handler._dynamodb.Table(handler.DYNAMODB_TABLE)

    def boom(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
            operation_name="UpdateItem",
        )

    monkeypatch.setattr(table, "update_item", boom)

    # Must NOT raise: status bookkeeping is best-effort.
    handler._mark_status(SONAR_HASH, handler.STATUS_FAILED, {"error": "x"})


def test_mark_status_writes_status_and_extra(handler):
    # Seed an in_flight row then mark it done with an extra attribute.
    handler._claim_job(SONAR_HASH, "PROJ-15")
    handler._mark_status(SONAR_HASH, handler.STATUS_DONE, {"pr_url": "https://x/pr/1"})

    item = handler._dynamodb.Table(handler.DYNAMODB_TABLE).items[SONAR_HASH]
    assert item["status"] == handler.STATUS_DONE
    assert item["pr_url"] == "https://x/pr/1"


# ---------------------------------------------------------------------------
# _invoke_runtime aggregation branches
# ---------------------------------------------------------------------------


def test_invoke_runtime_requires_arn(handler, monkeypatch):
    monkeypatch.setattr(handler, "AGENTCORE_RUNTIME_ARN", "")
    with pytest.raises(RuntimeError):
        handler._invoke_runtime({"sonar_hash": SONAR_HASH})


def test_invoke_runtime_aggregates_readable_body(handler, monkeypatch):
    monkeypatch.setattr(handler, "AGENTCORE_RUNTIME_ARN", "arn:aws:fake:runtime/x")
    stub = FakeAgentCoreClient(response_text='{"status": "ok"}')
    monkeypatch.setattr(handler, "_agentcore", stub)

    out = handler._invoke_runtime({"sonar_hash": SONAR_HASH})

    assert out == '{"status": "ok"}'
    # The session id is derived from the sonar_hash and is >= 33 chars.
    assert len(stub.calls) == 1
    assert stub.calls[0]["runtimeSessionId"].startswith(f"kiro-minions-{SONAR_HASH}")


def test_invoke_runtime_aggregates_bytes_body(handler, monkeypatch):
    monkeypatch.setattr(handler, "AGENTCORE_RUNTIME_ARN", "arn:aws:fake:runtime/x")

    class _BytesBodyClient:
        def invoke(self, **kwargs):
            return {"body": b"raw-bytes-response"}

    monkeypatch.setattr(handler, "_agentcore", _BytesBodyClient())

    out = handler._invoke_runtime({"sonar_hash": SONAR_HASH})
    assert out == "raw-bytes-response"


def test_invoke_runtime_aggregates_stream_chunks(handler, monkeypatch):
    monkeypatch.setattr(handler, "AGENTCORE_RUNTIME_ARN", "arn:aws:fake:runtime/x")

    class _StreamClient:
        def invoke(self, **kwargs):
            return {
                "stream": [
                    {"chunk": {"bytes": b"hello "}},
                    {"text": "world"},
                ]
            }

    monkeypatch.setattr(handler, "_agentcore", _StreamClient())

    out = handler._invoke_runtime({"sonar_hash": SONAR_HASH})
    assert out == "hello world"


# ---------------------------------------------------------------------------
# sqs_consumer body-shape branches
# ---------------------------------------------------------------------------


def test_sqs_consumer_accepts_dict_body(monkeypatch):
    seen: list = []
    monkeypatch.setattr(handler, "_invoke_runtime", lambda p: seen.append(p))

    # A record whose body is already a dict (not a JSON string).
    event = {"Records": [{"body": {"sonar_hash": SONAR_HASH, "ticket_id": "PROJ-15"}}]}

    result = handler.sqs_consumer(event, None)

    assert result["processed"] == 1
    assert seen[0]["sonar_hash"] == SONAR_HASH


def test_sqs_consumer_no_records_is_noop(monkeypatch):
    called: list = []
    monkeypatch.setattr(handler, "_invoke_runtime", lambda p: called.append(p))

    result = handler.sqs_consumer({}, None)

    assert result == {"status": "ok", "processed": 0}
    assert called == []
