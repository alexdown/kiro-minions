"""Handler-level tests for spec-executor ingress (FEAT-001).

Covers that handler_ide threads the ADR-002 Option C ``task_type`` discriminator
and the spec-tasks source fields through to the enqueued TaskPayload:

* a spec dispatch (task_type='spec' + tasks_md_path) returns 202 and the single
  enqueued payload carries task_type='spec' and the tasks_md_path;
* an inline spec dispatch (task_type='spec' + tasks_md) carries the inline body;
* a one_shot dispatch (and task_type omitted) carries task_type='one_shot' and
  no spec-only fields.

SDK stubs are installed by tests/conftest.py before these imports resolve. The
``handler`` fixture reloads the module against fresh fakes.
"""

import json

from fakes import make_ide_jwt_event  # noqa: E402 - conftest installs stubs first


REPO = "https://github.com/your-org/your-repo.git"
CLAIMS = {"sub": "dev-123", "email": "dev@customer.com"}
TASKS_PATH = ".kiro/specs/my-feature/tasks.md"


def _enqueued(handler) -> list[dict]:
    return [json.loads(m["MessageBody"]) for m in handler._sqs.messages]


def _spec_body(**overrides) -> dict:
    body = {
        "repo_clone_url": REPO,
        "summary": "Implement the whole spec",
        "task_type": "spec",
        "tasks_md_path": TASKS_PATH,
    }
    body.update(overrides)
    return body


def _one_shot_body(**overrides) -> dict:
    body = {
        "repo_clone_url": REPO,
        "description": "Add input validation to the login form.",
        "summary": "Validate login form input",
    }
    body.update(overrides)
    return body


def test_spec_dispatch_returns_202_and_carries_task_type_and_path(handler):
    event = make_ide_jwt_event(_spec_body(), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202

    payloads = _enqueued(handler)
    assert len(payloads) == 1
    sent = payloads[0]
    assert sent["source"] == "kiro-ide"
    assert sent["task_type"] == "spec"
    assert sent["tasks_md_path"] == TASKS_PATH
    # No inline body when only a path was supplied.
    assert "tasks_md" not in sent


def test_spec_dispatch_inline_tasks_md_carried(handler):
    inline = "- [ ] 1. Do the thing\n- [ ] 2. Do the next thing\n"
    # Supply only the inline body (no path).
    body = _spec_body(tasks_md=inline)
    del body["tasks_md_path"]
    event = make_ide_jwt_event(body, CLAIMS)

    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202

    sent = _enqueued(handler)[0]
    assert sent["task_type"] == "spec"
    assert sent["tasks_md"] == inline
    assert "tasks_md_path" not in sent


def test_one_shot_dispatch_carries_one_shot_and_no_spec_fields(handler):
    event = make_ide_jwt_event(_one_shot_body(), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202

    sent = _enqueued(handler)[0]
    assert sent["task_type"] == "one_shot"
    assert "tasks_md_path" not in sent
    assert "tasks_md" not in sent


def test_explicit_one_shot_task_type_carried(handler):
    event = make_ide_jwt_event(_one_shot_body(task_type="one_shot"), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202
    assert _enqueued(handler)[0]["task_type"] == "one_shot"


def test_spec_bad_tasks_md_path_returns_400(handler):
    event = make_ide_jwt_event(_spec_body(tasks_md_path="../../etc/passwd"), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 400
    assert _enqueued(handler) == []


def test_spec_without_tasks_source_returns_400(handler):
    body = _spec_body()
    del body["tasks_md_path"]
    event = make_ide_jwt_event(body, CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 400
    assert _enqueued(handler) == []


# ---------------------------------------------------------------------------
# Gate B (verify-vs-spec) acceptance inputs (ADR-008): threaded only when
# present so a payload without them stays byte-for-byte compatible.
# ---------------------------------------------------------------------------


def test_spec_threads_gate_b_inputs_when_present(handler):
    body = _spec_body(
        requirements_md_path=".kiro/specs/my-feature/requirements.md",
        design_md_path=".kiro/specs/my-feature/design.md",
    )
    event = make_ide_jwt_event(body, CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202

    sent = _enqueued(handler)[0]
    assert sent["requirements_md_path"] == ".kiro/specs/my-feature/requirements.md"
    assert sent["design_md_path"] == ".kiro/specs/my-feature/design.md"
    # Inline bodies not supplied -> omitted entirely.
    assert "requirements_md" not in sent
    assert "design_md" not in sent


def test_spec_threads_inline_gate_b_inputs(handler):
    body = _spec_body(
        requirements_md="# Requirements\n1.1 SHALL ...",
        design_md="# Design\narchitecture",
    )
    event = make_ide_jwt_event(body, CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202

    sent = _enqueued(handler)[0]
    assert sent["requirements_md"].startswith("# Requirements")
    assert sent["design_md"].startswith("# Design")


def test_spec_omits_gate_b_inputs_when_absent(handler):
    event = make_ide_jwt_event(_spec_body(), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202

    sent = _enqueued(handler)[0]
    for f in ("requirements_md_path", "design_md_path",
              "requirements_md", "design_md"):
        assert f not in sent


def test_one_shot_omits_gate_b_inputs_when_absent(handler):
    event = make_ide_jwt_event(_one_shot_body(), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 202

    sent = _enqueued(handler)[0]
    for f in ("requirements_md_path", "design_md_path",
              "requirements_md", "design_md"):
        assert f not in sent


def test_spec_bad_requirements_md_path_returns_400(handler):
    event = make_ide_jwt_event(
        _spec_body(requirements_md_path="../../etc/passwd"), CLAIMS)
    resp = handler.handler_ide(event, None)
    assert resp["statusCode"] == 400
    assert _enqueued(handler) == []
