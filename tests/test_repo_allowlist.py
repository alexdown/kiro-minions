"""Tests for issue #5: repo allowlist, enforced in the orchestrator and
re-checked in the runtime (defence in depth).

Covers:
  * parser.repo_allowed(): glob matching, allow-all on empty list, reject on
    non-match and on an unextractable URL.
  * handler.handler(): a disallowed repo is rejected 403 BEFORE any claim or
    SQS enqueue.
  * agent.run_pipeline(): a disallowed repo raises BEFORE any subprocess (the
    RecordingRunner records zero calls) when an allowlist is configured.
"""

import pytest

import agent  # noqa: E402 - conftest installs SDK stubs + sys.path first
import handler  # noqa: E402
import parser  # noqa: E402
from fakes import FakeSqsClient, RecordingRunner  # noqa: E402


# ---------------------------------------------------------------------------
# parser.repo_allowed() unit tests
# ---------------------------------------------------------------------------

ALLOWED_URL = "https://github.com/your-org/your-repo.git"
EVIL_URL = "https://github.com/evil-org/evil-repo.git"


def test_allow_all_when_empty():
    assert parser.repo_allowed(ALLOWED_URL, []) is True
    assert parser.repo_allowed(ALLOWED_URL, None) is True


def test_matches_exact_owner_repo():
    assert parser.repo_allowed(ALLOWED_URL, ["your-org/your-repo"]) is True


def test_matches_owner_glob():
    assert parser.repo_allowed(ALLOWED_URL, ["your-org/*"]) is True


def test_rejects_non_matching():
    assert parser.repo_allowed(EVIL_URL, ["your-org/*"]) is False


def test_match_is_case_insensitive():
    url = "https://github.com/Your-Org/Your-Repo.git"
    assert parser.repo_allowed(url, ["your-org/your-repo"]) is True


def test_rejects_unextractable_url_when_configured():
    # Not a github.com owner/repo shape -> rejected when an allowlist is set.
    assert parser.repo_allowed("not-a-url", ["your-org/*"]) is False


# ---------------------------------------------------------------------------
# handler.handler(): disallowed repo rejected before claim/enqueue
# ---------------------------------------------------------------------------

def _jira_body(clone_url: str) -> dict:
    description = (
        f"Clone: {clone_url}\n"
        "Base branch: master\n"
        "Suggested fix branch: sonarqube-fix/xss-swig-autoescape\n"
        "File to fix: server.js:135\n"
    )
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


def test_handler_rejects_disallowed_repo_before_claim_and_enqueue(monkeypatch):
    monkeypatch.setattr(handler, "REPO_ALLOWLIST", ["your-org/*"])
    monkeypatch.setattr(handler, "_claim_job",
                        lambda *a, **k: pytest.fail("claimed a disallowed repo"))
    sqs = FakeSqsClient()
    monkeypatch.setattr(handler, "SQS_QUEUE_URL", "https://sqs.fake/queue")
    monkeypatch.setattr(handler, "_sqs", sqs)

    resp = handler.handler(_jira_body(EVIL_URL), None)

    assert resp["statusCode"] == 403
    # No message was enqueued for a disallowed repo.
    assert sqs.messages == []


def test_handler_accepts_allowed_repo(monkeypatch):
    monkeypatch.setattr(handler, "REPO_ALLOWLIST", ["your-org/*"])
    monkeypatch.setattr(handler, "_claim_job", lambda h, t: True)
    monkeypatch.setattr(handler, "SQS_QUEUE_URL", "https://sqs.fake/queue")
    monkeypatch.setattr(handler, "_sqs", FakeSqsClient())

    resp = handler.handler(_jira_body(ALLOWED_URL), None)
    assert resp["statusCode"] == 202


# ---------------------------------------------------------------------------
# agent.run_pipeline(): disallowed repo fails before any subprocess
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {
    "ticket_id": "PROJ-15",
    "summary": "XSS fix",
    "description": "Enable autoescape",
    "ticket_url": "https://jira.example.com/browse/PROJ-15",
    "repo_clone_url": EVIL_URL,
    "base_branch": "master",
    "fix_branch": "sonarqube-fix/xss-swig-autoescape",
    "file_to_fix": "server.js:135",
    "sonar_hash": "6f50f92a-65f2-4c4c-aae5-9b069b44430a",
}


def test_runtime_rejects_disallowed_repo_before_subprocess(monkeypatch):
    monkeypatch.setenv("REPO_ALLOWLIST", "your-org/*")
    runner = RecordingRunner()
    monkeypatch.setattr(agent.subprocess, "run", runner)

    with pytest.raises(ValueError):
        agent.run_pipeline(dict(VALID_PAYLOAD))

    # No subprocess (clone/auth/anything) ran for a disallowed repo.
    assert runner.calls == []


def test_runtime_allows_when_allowlist_unset(monkeypatch):
    """Unset allowlist => allow (the validation step is a no-op)."""
    monkeypatch.delenv("REPO_ALLOWLIST", raising=False)
    # _validate_repo_allowed must not raise for the evil URL when unconfigured.
    agent._validate_repo_allowed(EVIL_URL)
