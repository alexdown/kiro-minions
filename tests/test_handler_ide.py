"""Tests for the Kiro IDE ingress entry point (FEAT-002).

Covers orchestrator/handler.py ``handler_ide`` end to end against the in-memory
fakes:

* happy path -> 202, writes an in_flight row keyed by the derived task_id (read
  back on the physical ``sonar_hash`` attribute), enqueues exactly one
  TaskPayload with source="kiro-ide" and actor from the JWT claim;
* missing/empty verified JWT claims -> 401;
* invalid clone URL / bad branch -> 400;
* repo not in allowlist -> 403;
* idempotent second dispatch of the same task -> 200 skipped, only one enqueue;
* client-supplied idempotency_key override drives the task_id;
* enqueue failure marks the task failed and returns a redacted 500.

SDK stubs for boto3/bedrock_agentcore are installed by tests/conftest.py before
these imports resolve. The ``handler`` fixture reloads the module against fresh
fakes and sets SQS_QUEUE_URL via ``aws_env``.
"""

import json

import pytest

from fakes import make_ide_jwt_event  # noqa: E402 - conftest installs stubs first


REPO = "https://github.com/your-org/your-repo.git"
CLAIMS = {"sub": "dev-123", "email": "dev@customer.com"}


def _ide_body(**overrides) -> dict:
    body = {
        "repo_clone_url": REPO,
        "description": "Add input validation to the login form.",
        "summary": "Validate login form input",
    }
    body.update(overrides)
    return body


def _enqueued(handler) -> list[dict]:
    return [json.loads(m["MessageBody"]) for m in handler._sqs.messages]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_ide_happy_path_returns_202(handler, jobs_table):
    event = make_ide_jwt_event(_ide_body(), CLAIMS)

    resp = handler.handler_ide(event, None)

    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["status"] == "accepted"
    assert body["actor"] == "dev-123"  # sub preferred over email
    task_id = body["task_id"]
    assert task_id.startswith("ide-")

    # In-flight row written, keyed by the derived task_id on sonar_hash attr.
    item = jobs_table.get_item(Key={"sonar_hash": task_id})["Item"]
    assert item["status"] == "in_flight"
    assert item["source"] == "kiro-ide"
    assert item["actor"] == "dev-123"
    assert "lease_expires_at" in item

    # Exactly one TaskPayload enqueued with the IDE shape.
    payloads = _enqueued(handler)
    assert len(payloads) == 1
    sent = payloads[0]
    assert sent["source"] == "kiro-ide"
    assert sent["actor"] == "dev-123"
    assert sent["repo_clone_url"] == REPO
    assert sent["base_branch"] == "main"
    assert sent["acceptance"] == "tests_pass"
    # The logical task_id is carried in the sonar_hash field (ADR-004/005).
    assert sent["sonar_hash"] == task_id
    assert sent["ticket_id"] == ""
    # No secret / raw runtime output in the response body.
    assert "runtime_response" not in body


def test_ide_actor_falls_back_to_email(handler):
    event = make_ide_jwt_event(_ide_body(), {"email": "only-email@x.com"})
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["actor"] == "only-email@x.com"


# ---------------------------------------------------------------------------
# Auth (401)
# ---------------------------------------------------------------------------


def test_ide_missing_claims_returns_401(handler):
    event = make_ide_jwt_event(_ide_body(), claims=None)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 401
    assert _enqueued(handler) == []


def test_ide_empty_claims_returns_401(handler):
    event = make_ide_jwt_event(_ide_body(), claims={"foo": "bar"})
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 401
    assert _enqueued(handler) == []


# ---------------------------------------------------------------------------
# Validation (400)
# ---------------------------------------------------------------------------


def test_ide_invalid_clone_url_returns_400(handler):
    event = make_ide_jwt_event(
        _ide_body(repo_clone_url="https://evil.example.com/x/y"), CLAIMS
    )
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 400
    assert _enqueued(handler) == []


def test_ide_bad_branch_returns_400(handler):
    event = make_ide_jwt_event(_ide_body(base_branch="bad branch!"), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 400
    assert _enqueued(handler) == []


def test_ide_missing_required_field_returns_400(handler):
    body = _ide_body()
    del body["description"]
    event = make_ide_jwt_event(body, CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 400
    assert _enqueued(handler) == []


def test_ide_unparseable_body_returns_400(handler):
    # Authenticated but the body is not valid JSON.
    event = make_ide_jwt_event("{not json", CLAIMS, json_string_body=False)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 400
    assert _enqueued(handler) == []


def test_ide_dynamo_error_returns_500(handler, monkeypatch):
    from botocore.exceptions import ClientError

    def boom(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "PutItem",
        )

    monkeypatch.setattr(handler, "_claim_job", boom)
    event = make_ide_jwt_event(_ide_body(), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 500
    assert _enqueued(handler) == []


# ---------------------------------------------------------------------------
# Allowlist (403)
# ---------------------------------------------------------------------------


def test_ide_repo_not_in_allowlist_returns_403(handler, monkeypatch):
    monkeypatch.setattr(handler, "REPO_ALLOWLIST", ["someone-else/*"])
    event = make_ide_jwt_event(_ide_body(), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 403
    # No claim, no enqueue when disallowed.
    assert _enqueued(handler) == []


def test_ide_repo_in_allowlist_allowed(handler, monkeypatch):
    monkeypatch.setattr(handler, "REPO_ALLOWLIST", ["your-org/*"])
    event = make_ide_jwt_event(_ide_body(), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202
    assert len(_enqueued(handler)) == 1


# ---------------------------------------------------------------------------
# Idempotency (200 skipped)
# ---------------------------------------------------------------------------


def test_ide_idempotent_second_dispatch_skips(handler):
    event = make_ide_jwt_event(_ide_body(), CLAIMS)

    first = handler.handler_ide(event, None)
    assert first["statusCode"] == 202

    second = handler.handler_ide(event, None)
    assert second["statusCode"] == 200
    assert json.loads(second["body"])["status"] == "skipped"

    # Only the first dispatch enqueued work.
    assert len(_enqueued(handler)) == 1


def test_ide_idempotency_key_override_drives_task_id(handler, jobs_table):
    event = make_ide_jwt_event(
        _ide_body(idempotency_key="my-key-xyz"), CLAIMS
    )
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["task_id"] == "my-key-xyz"
    # The claimed row is keyed by the client-supplied id.
    assert jobs_table.get_item(Key={"sonar_hash": "my-key-xyz"})["Item"]


# ---------------------------------------------------------------------------
# Enqueue failure (redacted 500)
# ---------------------------------------------------------------------------


def test_ide_enqueue_failure_marks_failed_and_500(handler, monkeypatch):
    marks: list = []
    monkeypatch.setattr(
        handler, "_mark_status",
        lambda key, status, extra=None: marks.append((key, status, extra)),
    )

    def boom(**kwargs):
        raise RuntimeError("sqs unavailable")

    handler._sqs.send_message = boom

    event = make_ide_jwt_event(_ide_body(), CLAIMS)
    resp = handler.handler_ide(event, None)

    assert resp["statusCode"] == 500
    assert marks and marks[0][1] == handler.STATUS_FAILED


# ---------------------------------------------------------------------------
# Direct-dict claims shape (test tolerance)
# ---------------------------------------------------------------------------


def test_ide_direct_dict_claims_shape(handler):
    # A direct-dict test event: top-level "claims" + "body" string.
    event = {"claims": CLAIMS, "body": json.dumps(_ide_body())}
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202


# ---------------------------------------------------------------------------
# Jira path backward compatibility: payload keeps its fields + source="jira"
# ---------------------------------------------------------------------------


_JIRA_DESCRIPTION = (
    "Clone: https://github.com/your-org/your-repo.git\n"
    "Base branch: master\n"
    "Suggested fix branch: sonarqube-fix/xss\n"
    "File to fix: server.js:135\n"
)


def _jira_event() -> dict:
    return {
        "issue": {
            "key": "PROJ-15",
            "fields": {
                "summary": "XSS fix",
                "description": _JIRA_DESCRIPTION,
                "labels": ["sonarqube", "sonar-hash:abc-123"],
            },
        }
    }


def test_jira_handler_still_emits_source_jira(handler):
    resp = handler.handler(_jira_event(), None)
    assert resp["statusCode"] == 202

    payloads = _enqueued(handler)
    assert len(payloads) == 1
    sent = payloads[0]
    # Additive fields.
    assert sent["source"] == "jira"
    assert sent["actor"] == ""
    # Every existing runtime-read field name is unchanged.
    assert sent["ticket_id"] == "PROJ-15"
    assert sent["sonar_hash"] == "abc-123"
    assert sent["repo_clone_url"] == "https://github.com/your-org/your-repo.git"
    assert sent["base_branch"] == "master"
    assert sent["fix_branch"] == "sonarqube-fix/xss"
    assert sent["file_to_fix"] == "server.js:135"
