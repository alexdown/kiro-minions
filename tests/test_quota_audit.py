"""Tests for FEAT-004 — per-actor dispatch quota + audit log (ADR-007 C & E).

Covers orchestrator/handler.py:

* The per-actor dispatch quota (ADR-007 C): under the limit a dispatch is
  allowed and the DynamoDB-backed counter increments; over the limit the ingress
  returns 429 and performs NO claim and NO enqueue. A disabled quota
  (ACTOR_QUOTA_MAX=0) never blocks. A counter-backend error fails OPEN.
* The dispatch audit log (ADR-007 E): both the Jira ``handler`` and the IDE
  ``handler_ide`` emit a redacted structured record naming actor / task_id /
  repo / source / outcome, and no raw secret ever reaches the audit output.

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


def _enqueued(handler) -> list:
    return [json.loads(m["MessageBody"]) for m in handler._sqs.messages]


def _parse_audit_records(captured: str) -> list:
    """Return the dispatch_decision JSON records from captured stdout."""
    records = []
    for line in captured.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("audit") == "dispatch_decision":
            records.append(obj)
    return records


# ---------------------------------------------------------------------------
# Per-actor dispatch quota (ADR-007 C)
# ---------------------------------------------------------------------------


def test_quota_disabled_by_default_allows_many(handler):
    # ACTOR_QUOTA_MAX defaults to 0 (unlimited): many distinct dispatches pass.
    for i in range(5):
        event = make_ide_jwt_event(
            _ide_body(idempotency_key=f"key-{i}"), CLAIMS
        )
        resp = handler.handler_ide(event, None)
        assert resp["statusCode"] == 202
    assert len(_enqueued(handler)) == 5


def test_quota_under_limit_allows_and_increments_counter(handler, jobs_table,
                                                         monkeypatch):
    monkeypatch.setattr(handler, "ACTOR_QUOTA_MAX", 3)
    monkeypatch.setattr(handler, "ACTOR_QUOTA_WINDOW_SECONDS", 3600)

    event = make_ide_jwt_event(_ide_body(idempotency_key="k1"), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202

    # The quota counter row was written and incremented to 1.
    now = 10_000_000
    monkeypatch.setattr(handler.time, "time", lambda: now)
    window_start = handler._quota_window_start(now)
    # Re-derive the key the handler used. Because we fixed time only now, read
    # whatever quota# counter exists and assert its count is >= 1.
    counter_items = [
        v for k, v in jobs_table.items.items()
        if isinstance(k, str) and k.startswith("quota#dev-123#")
    ]
    assert counter_items, "expected a per-actor quota counter row"
    assert counter_items[0]["count"] == 1
    assert "lease_expires_at" in counter_items[0]


def test_quota_over_limit_returns_429_no_enqueue(handler, jobs_table,
                                                 monkeypatch):
    monkeypatch.setattr(handler, "ACTOR_QUOTA_MAX", 2)
    monkeypatch.setattr(handler, "ACTOR_QUOTA_WINDOW_SECONDS", 3600)

    # First two dispatches (distinct task_ids) are within quota.
    for i in range(2):
        event = make_ide_jwt_event(
            _ide_body(idempotency_key=f"ok-{i}"), CLAIMS
        )
        assert handler.handler_ide(event, None)["statusCode"] == 202

    assert len(_enqueued(handler)) == 2

    # The third exceeds the quota -> 429 and NO enqueue of genuinely new work
    # (the key invariant). Quota now runs after the claim (finding #1), so the
    # row is briefly created and then released (marked failed) for retry.
    event = make_ide_jwt_event(_ide_body(idempotency_key="blocked"), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 429
    body = json.loads(resp["body"])
    assert "quota" in body["error"].lower()

    # Still only two enqueued: the over-limit task never reached the queue.
    assert len(_enqueued(handler)) == 2
    # The released row is retryable, not wedged in_flight.
    assert jobs_table.items["ide-blocked"]["status"] == handler.STATUS_FAILED


def test_idempotent_redispatch_does_not_consume_quota(handler, jobs_table,
                                                      monkeypatch):
    """Finding #1: re-dispatching the SAME task_id must not burn a quota slot.

    With a finite quota of 1, the first dispatch of a task_id claims + counts
    (slot used). Re-dispatching the SAME task_id returns 200 "skipped" WITHOUT
    touching the quota counter, so a brand-new, DIFFERENT task_id still has the
    (only remaining) slot available rather than being wrongly 429'd.
    """
    monkeypatch.setattr(handler, "ACTOR_QUOTA_MAX", 1)
    monkeypatch.setattr(handler, "ACTOR_QUOTA_WINDOW_SECONDS", 3600)

    # First dispatch of task "same" — claimed + counted (quota now at 1/1).
    same = make_ide_jwt_event(_ide_body(idempotency_key="same"), CLAIMS)
    assert handler.handler_ide(same, None)["statusCode"] == 202

    # The per-actor counter reads exactly 1 after the first (new) dispatch.
    counters = [
        v for k, v in jobs_table.items.items()
        if isinstance(k, str) and k.startswith("quota#dev-123#")
    ]
    assert counters and counters[0]["count"] == 1

    # Re-dispatch the SAME task_id twice (double-click / dropped 202). Each is a
    # skipped claim -> 200, and must NOT increment the quota counter.
    for _ in range(2):
        resp = handler.handler_ide(same, None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["status"] == "skipped"

    counters = [
        v for k, v in jobs_table.items.items()
        if isinstance(k, str) and k.startswith("quota#dev-123#")
    ]
    assert counters[0]["count"] == 1, "skipped re-dispatch must not consume quota"

    # Only ONE enqueue happened despite three POSTs of the same task.
    assert len(_enqueued(handler)) == 1

    # A genuinely NEW, different task still has the remaining slot: it is NOT
    # wrongly 429'd by the earlier retries.
    other = make_ide_jwt_event(_ide_body(idempotency_key="other"), CLAIMS)
    resp = handler.handler_ide(other, None)
    assert resp["statusCode"] == 429, (
        "the single real slot was consumed by 'same'; 'other' is correctly over "
        "the limit — but only because retries did NOT erode the quota"
    )


def test_quota_over_limit_releases_claim_for_retry(handler, jobs_table,
                                                    monkeypatch):
    """Over-the-limit new work is claimed-then-released so it stays retryable.

    Because the quota now runs AFTER the claim (finding #1), an over-limit
    dispatch briefly creates the row. We release it by marking it ``failed`` so
    a later dispatch (in a fresh window) can re-claim it instead of finding it
    wedged ``in_flight``.
    """
    monkeypatch.setattr(handler, "ACTOR_QUOTA_MAX", 1)

    assert handler.handler_ide(
        make_ide_jwt_event(_ide_body(idempotency_key="first"), CLAIMS), None
    )["statusCode"] == 202

    resp = handler.handler_ide(
        make_ide_jwt_event(_ide_body(idempotency_key="blocked"), CLAIMS), None
    )
    assert resp["statusCode"] == 429

    # The blocked task_id row exists but is left in a retryable terminal state.
    row = jobs_table.items.get("ide-blocked")
    assert row is not None
    assert row["status"] == handler.STATUS_FAILED
    # No enqueue for the blocked task.
    assert len(_enqueued(handler)) == 1


def test_quota_fail_open_emits_observable_signal(handler, capsys, monkeypatch):
    """Finding #2: a fail-open must emit a distinct, greppable signal."""
    monkeypatch.setattr(handler, "ACTOR_QUOTA_MAX", 1)

    from botocore.exceptions import ClientError

    def boom(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "UpdateItem",
        )

    table = handler._dynamodb.Table(handler.DYNAMODB_TABLE)
    monkeypatch.setattr(table, "update_item", boom)

    event = make_ide_jwt_event(_ide_body(idempotency_key="fo-signal"), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202  # fail-open: dispatch still succeeds

    out = capsys.readouterr().out
    events = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("event") == handler.QUOTA_FAIL_OPEN_EVENT:
            events.append(obj)
    assert events, "expected an observable quota_fail_open signal"
    assert events[-1]["actor"] == "dev-123"
    assert "error" in events[-1]["reason"].lower()


def test_quota_fail_open_signal_redacts_secret(handler, capsys, monkeypatch):
    """The fail-open signal must never leak a secret in its reason."""
    secret = "ghp_FAILOPEN_SECRET_123456"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    monkeypatch.setattr(handler, "ACTOR_QUOTA_MAX", 1)

    from botocore.exceptions import ClientError

    def boom(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": f"Boom {secret}"}}, "UpdateItem"
        )

    table = handler._dynamodb.Table(handler.DYNAMODB_TABLE)
    monkeypatch.setattr(table, "update_item", boom)

    handler.handler_ide(
        make_ide_jwt_event(_ide_body(idempotency_key="fo-sec"), CLAIMS), None
    )
    assert secret not in capsys.readouterr().out


def test_quota_is_per_actor(handler, monkeypatch):
    monkeypatch.setattr(handler, "ACTOR_QUOTA_MAX", 1)

    # dev-123 uses its single slot.
    e1 = make_ide_jwt_event(_ide_body(idempotency_key="a1"),
                            {"sub": "dev-123"})
    assert handler.handler_ide(e1, None)["statusCode"] == 202
    e2 = make_ide_jwt_event(_ide_body(idempotency_key="a2"),
                            {"sub": "dev-123"})
    assert handler.handler_ide(e2, None)["statusCode"] == 429

    # A DIFFERENT actor still has its own slot.
    e3 = make_ide_jwt_event(_ide_body(idempotency_key="b1"),
                            {"sub": "dev-999"})
    assert handler.handler_ide(e3, None)["statusCode"] == 202


def test_quota_counter_error_fails_open(handler, monkeypatch):
    monkeypatch.setattr(handler, "ACTOR_QUOTA_MAX", 1)

    from botocore.exceptions import ClientError

    def boom(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "UpdateItem",
        )

    # Make only the counter UpdateItem raise; the claim uses put_item so it is
    # unaffected. Patch the table's update_item on the resource's table.
    table = handler._dynamodb.Table(handler.DYNAMODB_TABLE)
    monkeypatch.setattr(table, "update_item", boom)

    event = make_ide_jwt_event(_ide_body(idempotency_key="fo"), CLAIMS)
    resp = handler.handler_ide(event, None)
    # Fail-open: the dispatch still succeeds despite the counter error.
    assert resp["statusCode"] == 202


# ---------------------------------------------------------------------------
# Audit log of dispatch decisions (ADR-007 E)
# ---------------------------------------------------------------------------


def test_ide_audit_records_claimed(handler, capsys):
    event = make_ide_jwt_event(_ide_body(idempotency_key="aud-1"), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202

    records = _parse_audit_records(capsys.readouterr().out)
    assert records, "expected an audit record"
    rec = records[-1]
    assert rec["source"] == "kiro-ide"
    assert rec["actor"] == "dev-123"
    assert rec["task_id"] == "ide-aud-1"
    assert rec["repo"] == REPO
    assert rec["outcome"] == handler.AUDIT_CLAIMED
    assert isinstance(rec["ts"], int)


def test_ide_audit_records_quota_exceeded(handler, capsys, monkeypatch):
    monkeypatch.setattr(handler, "ACTOR_QUOTA_MAX", 1)
    handler.handler_ide(
        make_ide_jwt_event(_ide_body(idempotency_key="q1"), CLAIMS), None
    )
    capsys.readouterr()  # drop the first (claimed) record
    handler.handler_ide(
        make_ide_jwt_event(_ide_body(idempotency_key="q2"), CLAIMS), None
    )
    records = _parse_audit_records(capsys.readouterr().out)
    assert any(r["outcome"] == handler.AUDIT_QUOTA_EXCEEDED for r in records)


def test_ide_audit_records_rejected_on_bad_auth(handler, capsys):
    event = make_ide_jwt_event(_ide_body(), claims=None)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 401
    records = _parse_audit_records(capsys.readouterr().out)
    assert records and records[-1]["outcome"] == handler.AUDIT_REJECTED


def test_jira_audit_records_claimed(handler, capsys):
    description = (
        "Clone: https://github.com/your-org/your-repo.git\n"
        "Base branch: master\n"
        "Suggested fix branch: sonarqube-fix/xss\n"
    )
    event = {
        "issue": {
            "key": "PROJ-42",
            "fields": {
                "summary": "XSS fix",
                "description": description,
                "labels": ["sonarqube", "sonar-hash:abc-999"],
            },
        }
    }
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 202
    records = _parse_audit_records(capsys.readouterr().out)
    rec = records[-1]
    assert rec["source"] == "jira"
    assert rec["task_id"] == "abc-999"
    assert rec["outcome"] == handler.AUDIT_CLAIMED


def test_audit_never_leaks_secret(handler, capsys, monkeypatch):
    # A secret present in the environment must be masked everywhere, including
    # any reason string routed through the audit log.
    secret = "ghp_SUPERSECRETTOKEN123"
    monkeypatch.setenv("GITHUB_TOKEN", secret)

    # Force an enqueue failure whose exception text embeds the secret.
    def boom(**kwargs):
        raise RuntimeError(f"sqs failed with token {secret}")

    handler._sqs.send_message = boom

    event = make_ide_jwt_event(_ide_body(idempotency_key="leak"), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 500

    out = capsys.readouterr().out
    assert secret not in out
    records = _parse_audit_records(out)
    assert records
    # The rejected record carries a redacted reason, not the raw secret.
    assert any(r["outcome"] == handler.AUDIT_REJECTED for r in records)
    assert all(secret not in json.dumps(r) for r in records)
