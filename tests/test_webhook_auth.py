"""Tests for issue #5: webhook authentication (shared-secret / HMAC).

Covers:
  * verify_webhook() directly: accepts a correct HMAC signature and a correct
    shared-secret header; rejects a wrong/missing credential (fail closed) when
    WEBHOOK_SECRET is configured; no-ops when WEBHOOK_SECRET is unset.
  * handler.handler() returns 401/403 for an unauthenticated webhook when the
    secret is configured, 202 for a valid one, and the secret VALUE never leaks
    into the response body.

All comparisons in verify_webhook use hmac.compare_digest (constant-time). The
secret is wired through a module-level WEBHOOK_SECRET that we monkeypatch, in
the same style as tests/test_secret_redaction.py (which monkeypatches
handler.SQS_QUEUE_URL / handler._claim_job directly).
"""

import hashlib
import hmac
import json

import pytest

import handler  # noqa: E402 - conftest installs SDK stubs + sys.path first
from fakes import FakeSqsClient  # noqa: E402


SECRET_SENTINEL = "whk_SENTINEL_SECRET_abcdef0123456789"


VALID_DESCRIPTION = (
    "Clone: https://github.com/your-org/your-repo.git\n"
    "Base branch: master\n"
    "Suggested fix branch: sonarqube-fix/xss-swig-autoescape\n"
    "File to fix: server.js:135\n"
)


def _jira_body(description: str = VALID_DESCRIPTION) -> dict:
    return {
        "issue": {
            "key": "PROJ-15",
            "fields": {
                "summary": "XSS fix",
                "description": description,
                "labels": [
                    "sonarqube",
                    "sonar-hash:6f50f92a-65f2-4c4c-aae5-9b069b44430a",
                ],
            },
        }
    }


def _proxy_event(raw_body: str, headers: dict) -> dict:
    """An API Gateway HTTP API proxy event with a raw string body."""
    return {"body": raw_body, "headers": headers}


def _sign(secret: str, raw_body: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------
# verify_webhook() unit tests
# ---------------------------------------------------------------------------


def test_verify_noop_when_secret_unset(monkeypatch):
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", "")
    # Any body/headers are accepted (fail-open) when no secret is configured.
    handler.verify_webhook("{}", {"x-webhook-secret": "anything"})


def test_verify_noop_for_direct_invoke(monkeypatch):
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    # No raw body (direct/local invoke) => trusted even with a secret configured.
    handler.verify_webhook(None, {})


def test_verify_accepts_correct_hmac(monkeypatch):
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    raw = json.dumps(_jira_body())
    sig = _sign(SECRET_SENTINEL, raw)
    handler.verify_webhook(raw, {"x-hub-signature-256": f"sha256={sig}"})
    # Also accepts a bare hex signature with no sha256= prefix.
    handler.verify_webhook(raw, {"x-hub-signature-256": sig})


def test_verify_accepts_uppercase_hex_hmac(monkeypatch):
    """Review finding #3: an UPPERCASE-hex signature is accepted (case-folded).

    hexdigest() is lowercase; a non-GitHub sender supplying uppercase hex would
    otherwise be rejected 403. Hex is case-insensitive so folding the provided
    value before compare_digest does not weaken the check.
    """
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    raw = json.dumps(_jira_body())
    sig_upper = _sign(SECRET_SENTINEL, raw).upper()
    # Uppercase, with and without the sha256= prefix (prefix match is also
    # case-insensitive via .lower().startswith).
    handler.verify_webhook(raw, {"x-hub-signature-256": f"sha256={sig_upper}"})
    handler.verify_webhook(raw, {"x-hub-signature-256": sig_upper})


def test_verify_rejects_wrong_hmac(monkeypatch):
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    raw = json.dumps(_jira_body())
    with pytest.raises(handler.WebhookAuthError) as exc:
        handler.verify_webhook(raw, {"x-hub-signature-256": "sha256=deadbeef"})
    assert exc.value.status_code == 403


def test_verify_accepts_correct_shared_secret(monkeypatch):
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    handler.verify_webhook("{}", {"x-webhook-secret": SECRET_SENTINEL})
    handler.verify_webhook("{}", {"x-webhook-token": SECRET_SENTINEL})


def test_verify_rejects_wrong_shared_secret(monkeypatch):
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    with pytest.raises(handler.WebhookAuthError) as exc:
        handler.verify_webhook("{}", {"x-webhook-secret": "wrong-secret"})
    assert exc.value.status_code == 403


def test_verify_missing_credential_is_401(monkeypatch):
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    with pytest.raises(handler.WebhookAuthError) as exc:
        handler.verify_webhook("{}", {})
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# handler.handler() integration
# ---------------------------------------------------------------------------


def test_handler_rejects_unauthenticated_webhook(monkeypatch):
    """A configured secret with no credential header => 401, no secret leak."""
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    # Guard: a claim/enqueue must never be reached for an unauthenticated call.
    monkeypatch.setattr(handler, "_claim_job",
                        lambda *a, **k: pytest.fail("claimed before auth"))

    raw = json.dumps(_jira_body())
    resp = handler.handler(_proxy_event(raw, {}), None)

    assert resp["statusCode"] == 401
    assert SECRET_SENTINEL not in resp["body"]


def test_handler_rejects_invalid_signature(monkeypatch):
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    monkeypatch.setattr(handler, "_claim_job",
                        lambda *a, **k: pytest.fail("claimed before auth"))

    raw = json.dumps(_jira_body())
    resp = handler.handler(
        _proxy_event(raw, {"x-hub-signature-256": "sha256=bad"}), None
    )

    assert resp["statusCode"] == 403
    assert SECRET_SENTINEL not in resp["body"]


def test_handler_accepts_valid_signature(monkeypatch):
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    monkeypatch.setattr(handler, "_claim_job", lambda h, t: True)
    monkeypatch.setattr(handler, "SQS_QUEUE_URL", "https://sqs.fake/queue")
    monkeypatch.setattr(handler, "_sqs", FakeSqsClient())

    raw = json.dumps(_jira_body())
    sig = _sign(SECRET_SENTINEL, raw)
    resp = handler.handler(
        _proxy_event(raw, {"x-hub-signature-256": f"sha256={sig}"}), None
    )

    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["status"] == "accepted"
    assert SECRET_SENTINEL not in resp["body"]


def test_handler_direct_invoke_allowed_with_secret_configured(monkeypatch):
    """A raw-dict (direct) invoke has no raw body/signature => still accepted."""
    monkeypatch.setattr(handler, "WEBHOOK_SECRET", SECRET_SENTINEL)
    monkeypatch.setattr(handler, "_claim_job", lambda h, t: True)
    monkeypatch.setattr(handler, "SQS_QUEUE_URL", "https://sqs.fake/queue")
    monkeypatch.setattr(handler, "_sqs", FakeSqsClient())

    resp = handler.handler(_jira_body(), None)
    assert resp["statusCode"] == 202
    assert SECRET_SENTINEL not in resp["body"]
