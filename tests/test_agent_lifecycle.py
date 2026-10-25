"""Tests for FEAT-004 runtime lifecycle (#6) and async offload (#14).

Covers runtime/agent.py:

* ``write_job_status`` writes the expected terminal item to DynamoDB and never
  embeds a secret; a write failure is swallowed (best-effort).
* The entrypoint writes ``done`` + pr_url on success, ``tests_failed`` + a
  redacted reason on the no-change path, and ``failed`` + a redacted reason when
  the pipeline raises.
* The entrypoint runs the blocking pipeline via ``asyncio.to_thread`` so the
  event loop (and /ping) stays responsive (#14).

SDK stubs are installed by tests/conftest.py before these imports resolve.
"""

import asyncio
import threading

import pytest

import agent  # noqa: E402 - conftest installs SDK stubs first
from fakes import FakeDynamoResource  # noqa: E402


SONAR_HASH = "6f50f92a-65f2-4c4c-aae5-9b069b44430a"
TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_abcdef0123456789"


def _use_fresh_table(monkeypatch) -> FakeDynamoResource:
    resource = FakeDynamoResource()
    monkeypatch.setattr(agent, "_dynamodb", resource)
    return resource


def _payload() -> dict:
    return {
        "ticket_id": "PROJ-15",
        "sonar_hash": SONAR_HASH,
        "repo_clone_url": "https://github.com/your-org/your-repo.git",
        "base_branch": "master",
        "fix_branch": "sonarqube-fix/xss",
        "description": "Enable autoescape",
        "summary": "XSS fix",
    }


async def _drain(gen) -> list:
    """Collect all items yielded by an async generator."""
    out = []
    async for item in gen:
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# write_job_status
# ---------------------------------------------------------------------------


def test_write_job_status_done(monkeypatch):
    resource = _use_fresh_table(monkeypatch)

    agent.write_job_status(SONAR_HASH, agent.STATUS_DONE,
                           {"pr_url": "https://github.com/o/r/pull/1"})

    item = resource.Table(agent.DYNAMODB_TABLE).items[SONAR_HASH]
    assert item["status"] == "done"
    assert item["pr_url"] == "https://github.com/o/r/pull/1"
    assert "updated_at" in item


def test_write_job_status_failed_with_reason(monkeypatch):
    resource = _use_fresh_table(monkeypatch)

    agent.write_job_status(SONAR_HASH, agent.STATUS_FAILED,
                           {"reason": "boom"})

    item = resource.Table(agent.DYNAMODB_TABLE).items[SONAR_HASH]
    assert item["status"] == "failed"
    assert item["reason"] == "boom"


def test_write_job_status_best_effort_swallows_errors(monkeypatch):
    class ExplodingResource:
        def Table(self, name):  # noqa: N802 - boto3 API name
            raise RuntimeError("dynamo down")

    monkeypatch.setattr(agent, "_dynamodb", ExplodingResource())
    # Must not raise.
    agent.write_job_status(SONAR_HASH, agent.STATUS_DONE, {"pr_url": "x"})


# ---------------------------------------------------------------------------
# Entrypoint -> terminal status
# ---------------------------------------------------------------------------


def test_entrypoint_writes_done_on_success(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    monkeypatch.setattr(
        agent, "run_pipeline",
        lambda p: {"status": "success",
                   "pr_url": "https://github.com/o/r/pull/7",
                   "tests_passed": True, "attempts": 1},
    )

    asyncio.run(_drain(agent.entrypoint(_payload())))

    item = resource.Table(agent.DYNAMODB_TABLE).items[SONAR_HASH]
    assert item["status"] == "done"
    assert item["pr_url"] == "https://github.com/o/r/pull/7"


def test_entrypoint_writes_tests_failed_on_no_change(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    monkeypatch.setattr(
        agent, "run_pipeline",
        lambda p: {"status": "failed",
                   "reason": "kiro-cli produced no changes",
                   "attempts": 3},
    )

    asyncio.run(_drain(agent.entrypoint(_payload())))

    item = resource.Table(agent.DYNAMODB_TABLE).items[SONAR_HASH]
    assert item["status"] == "tests_failed"
    assert item["reason"] == "kiro-cli produced no changes"


def test_entrypoint_writes_failed_with_redacted_reason_on_exception(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)

    def boom(payload):
        raise RuntimeError(f"clone failed using {TOKEN_SENTINEL}")

    monkeypatch.setattr(agent, "run_pipeline", boom)

    asyncio.run(_drain(agent.entrypoint(_payload())))

    item = resource.Table(agent.DYNAMODB_TABLE).items[SONAR_HASH]
    assert item["status"] == "failed"
    # The reason is redacted — the token never reaches the written item.
    assert TOKEN_SENTINEL not in item["reason"]
    assert agent.REDACTION_MASK in item["reason"]


def test_entrypoint_written_item_never_contains_secret(monkeypatch):
    resource = _use_fresh_table(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setattr(
        agent, "run_pipeline",
        lambda p: {"status": "failed",
                   "reason": f"leaked {TOKEN_SENTINEL}",
                   "attempts": 3},
    )

    asyncio.run(_drain(agent.entrypoint(_payload())))

    item = resource.Table(agent.DYNAMODB_TABLE).items[SONAR_HASH]
    for value in item.values():
        assert TOKEN_SENTINEL not in str(value)


# ---------------------------------------------------------------------------
# #14: run_pipeline runs via asyncio.to_thread (off the event loop)
# ---------------------------------------------------------------------------


def test_entrypoint_offloads_pipeline_via_to_thread(monkeypatch):
    _use_fresh_table(monkeypatch)

    main_thread = threading.get_ident()
    record = {"to_thread_called": False, "pipeline_thread": None}

    def quick_pipeline(payload):
        record["pipeline_thread"] = threading.get_ident()
        return {"status": "success", "pr_url": "https://github.com/o/r/pull/1"}

    monkeypatch.setattr(agent, "run_pipeline", quick_pipeline)

    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        record["to_thread_called"] = True
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(agent.asyncio, "to_thread", spy_to_thread)

    asyncio.run(_drain(agent.entrypoint(_payload())))

    assert record["to_thread_called"] is True
    # The pipeline executed on a worker thread, not the event-loop thread.
    assert record["pipeline_thread"] is not None
    assert record["pipeline_thread"] != main_thread
