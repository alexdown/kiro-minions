"""Tests for the pure IDE request mapper (FEAT-002).

Covers orchestrator/parser.py ``parse_ide_request`` / ``derive_task_id``:

* A valid IDE task request maps to the TaskPayload field subset with
  source="kiro-ide", acceptance="tests_pass", actor from the caller.
* base_branch defaults to "main"; fix_branch is derived from the task_id when
  absent and validated when present.
* The derived task_id is a stable "ide-" hash of actor+repo+base+description.
* A client-supplied idempotency_key overrides the derived task_id.
* Missing/invalid required fields and bad clone URL / branch raise
  IdeRequestError (-> HTTP 400 at the handler layer).

These are pure functions with no AWS dependency; conftest puts orchestrator/ on
sys.path so ``import parser`` resolves directly.
"""

import pytest

import parser  # noqa: E402 - conftest extends sys.path


ACTOR = "dev@customer.com"
REPO = "https://github.com/your-org/your-repo.git"


def _request(**overrides) -> dict:
    body = {
        "repo_clone_url": REPO,
        "description": "Refactor the retry loop to use exponential backoff.",
        "summary": "Exponential backoff for retries",
    }
    body.update(overrides)
    return body


def test_happy_path_maps_to_task_payload_fields():
    mapped = parser.parse_ide_request(_request(), ACTOR)

    assert mapped["source"] == "kiro-ide"
    assert mapped["actor"] == ACTOR
    assert mapped["repo_clone_url"] == REPO
    assert mapped["base_branch"] == "main"  # default
    assert mapped["description"].startswith("Refactor")
    assert mapped["summary"] == "Exponential backoff for retries"
    assert mapped["acceptance"] == "tests_pass"
    assert mapped["file_to_fix"] is None
    assert mapped["task_id"].startswith("ide-")
    # Derived fix branch is validated and references the task_id fragment with
    # the redundant ``ide-`` prefix stripped (task_id already carries it).
    fragment = mapped["task_id"][len("ide-"):]
    assert mapped["fix_branch"] == f"kiro/ide-{fragment[:12]}"


def test_optional_fields_passed_through():
    mapped = parser.parse_ide_request(
        _request(
            base_branch="develop",
            file_to_fix="src/foo.ts:42",
            fix_branch="feature/my-branch",
        ),
        ACTOR,
    )
    assert mapped["base_branch"] == "develop"
    assert mapped["file_to_fix"] == "src/foo.ts:42"
    assert mapped["fix_branch"] == "feature/my-branch"


def test_task_id_is_stable_for_same_inputs():
    a = parser.parse_ide_request(_request(), ACTOR)
    b = parser.parse_ide_request(_request(), ACTOR)
    assert a["task_id"] == b["task_id"]


def test_task_id_changes_with_actor():
    a = parser.parse_ide_request(_request(), "a@x.com")
    b = parser.parse_ide_request(_request(), "b@x.com")
    assert a["task_id"] != b["task_id"]


def test_derive_task_id_prefix_and_shape():
    tid = parser.derive_task_id(ACTOR, REPO, "main", "do the thing")
    assert tid.startswith("ide-")
    assert len(tid) == len("ide-") + 32


def test_idempotency_key_override():
    mapped = parser.parse_ide_request(
        _request(idempotency_key="client-supplied-123"), ACTOR
    )
    # The override is namespaced under ``ide-`` (finding #3): a bare key is
    # prefixed so it can never collide with a Jira hash or a quota# counter.
    assert mapped["task_id"] == "ide-client-supplied-123"
    # Derived fix branch follows the overridden task_id (prefix stripped once).
    assert mapped["fix_branch"] == "kiro/ide-client-suppl"


def test_idempotency_key_with_ide_prefix_is_idempotent():
    # A caller that echoes a previously-returned task_id (already ``ide-``
    # prefixed) is accepted without double-prefixing.
    mapped = parser.parse_ide_request(
        _request(idempotency_key="ide-client-supplied-123"), ACTOR
    )
    assert mapped["task_id"] == "ide-client-supplied-123"


def test_idempotency_key_rejects_quota_prefix():
    # A client must not be able to target the per-actor quota counter namespace.
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(
            _request(idempotency_key="quota#dev-123#999"), ACTOR
        )


def test_idempotency_key_rejects_hash_character():
    # ``#`` is excluded from the charset so the key cannot enter any ``x#y``
    # namespace (quota counters, or any future shared-table convention).
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(
            _request(idempotency_key="ide-foo#bar"), ACTOR
        )


def test_idempotency_key_rejects_overlong():
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(
            _request(idempotency_key="x" * 200), ACTOR
        )


def test_idempotency_key_rejects_whitespace_and_slash():
    for bad in ("has space", "has/slash", "has\ttab"):
        with pytest.raises(parser.IdeRequestError):
            parser.parse_ide_request(_request(idempotency_key=bad), ACTOR)


def test_idempotency_key_empty_rejected():
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(_request(idempotency_key="   "), ACTOR)


@pytest.mark.parametrize("field", ["repo_clone_url", "description", "summary"])
def test_missing_required_field_rejected(field):
    body = _request()
    del body[field]
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(body, ACTOR)


@pytest.mark.parametrize("field", ["repo_clone_url", "description", "summary"])
def test_empty_required_field_rejected(field):
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(_request(**{field: "   "}), ACTOR)


def test_invalid_clone_url_rejected():
    with pytest.raises(ValueError):
        parser.parse_ide_request(
            _request(repo_clone_url="https://evil.example.com/x/y"), ACTOR
        )


def test_invalid_base_branch_rejected():
    with pytest.raises(ValueError):
        parser.parse_ide_request(_request(base_branch="bad branch!"), ACTOR)


def test_invalid_fix_branch_rejected():
    with pytest.raises(ValueError):
        parser.parse_ide_request(_request(fix_branch="../escape"), ACTOR)


def test_missing_actor_rejected():
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(_request(), "")


def test_non_dict_body_rejected():
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(None, ACTOR)


# ---------------------------------------------------------------------------
# task_type discriminator (ADR-002 Option C): one_shot (default) vs spec
# ---------------------------------------------------------------------------


def test_task_type_defaults_to_one_shot():
    mapped = parser.parse_ide_request(_request(), ACTOR)
    assert mapped["task_type"] == "one_shot"
    # Spec-only fields absent for a one_shot job.
    assert mapped["tasks_md_path"] is None
    assert mapped["tasks_md"] is None


def test_explicit_one_shot_keeps_required_fields():
    mapped = parser.parse_ide_request(_request(task_type="one_shot"), ACTOR)
    assert mapped["task_type"] == "one_shot"
    assert mapped["description"].startswith("Refactor")
    assert mapped["summary"] == "Exponential backoff for retries"


def test_one_shot_still_requires_description():
    body = _request(task_type="one_shot")
    del body["description"]
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(body, ACTOR)


def test_invalid_task_type_rejected():
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(_request(task_type="open_ended"), ACTOR)


def _spec_request(**overrides) -> dict:
    body = {
        "repo_clone_url": REPO,
        "summary": "Implement the whole spec",
        "task_type": "spec",
    }
    body.update(overrides)
    return body


def test_spec_with_tasks_md_path_maps_through():
    mapped = parser.parse_ide_request(
        _spec_request(tasks_md_path=".kiro/specs/my-feature/tasks.md"), ACTOR
    )
    assert mapped["task_type"] == "spec"
    assert mapped["tasks_md_path"] == ".kiro/specs/my-feature/tasks.md"
    assert mapped["tasks_md"] is None
    # summary still required and carried; description optional (empty here).
    assert mapped["summary"] == "Implement the whole spec"
    assert mapped["description"] == ""
    # Still gets a task_id + validated fix branch + the IDE shape.
    assert mapped["task_id"].startswith("ide-")
    assert mapped["source"] == "kiro-ide"
    assert mapped["acceptance"] == "tests_pass"


def test_spec_with_inline_tasks_md_carried():
    inline = "- [ ] 1. Do the thing\n- [ ] 2. Do the next thing\n"
    mapped = parser.parse_ide_request(_spec_request(tasks_md=inline), ACTOR)
    assert mapped["task_type"] == "spec"
    assert mapped["tasks_md"] == inline
    assert mapped["tasks_md_path"] is None


def test_spec_with_optional_description_carried():
    mapped = parser.parse_ide_request(
        _spec_request(
            tasks_md_path=".kiro/specs/x/tasks.md",
            description="Extra guidance for the whole spec.",
        ),
        ACTOR,
    )
    assert mapped["description"] == "Extra guidance for the whole spec."


def test_spec_without_any_tasks_source_rejected():
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(_spec_request(), ACTOR)


def test_spec_still_requires_summary():
    body = _spec_request(tasks_md_path=".kiro/specs/x/tasks.md")
    del body["summary"]
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(body, ACTOR)


def test_spec_empty_inline_tasks_md_rejected():
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(_spec_request(tasks_md="   "), ACTOR)


# ---------------------------------------------------------------------------
# tasks_md_path traversal / shape rejection (security control)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",            # absolute
        "../etc/passwd",          # leading parent traversal
        "a/../b",                 # embedded parent traversal
        "a/../../b",              # deeper traversal
        "..",                     # bare parent
        "-rf",                    # leading dash (option injection)
        "a b/tasks.md",           # whitespace
        "a\\b/tasks.md",          # backslash
        "a/tasks.md\x00",         # NUL
        "a/dir/",                 # trailing slash (directory)
        ".",                      # bare dot
        "x" * 300,                # over the length cap
    ],
)
def test_spec_rejects_bad_tasks_md_path(bad_path):
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(_spec_request(tasks_md_path=bad_path), ACTOR)


def test_validate_repo_relative_path_accepts_normal_path():
    good = ".kiro/specs/my-feature/tasks.md"
    assert parser.validate_repo_relative_path(good) == good


@pytest.mark.parametrize("bad", ["/abs", "../x", "a/../b", "a b", "a\\b", ".."])
def test_validate_repo_relative_path_rejects(bad):
    with pytest.raises(parser.IdeRequestError):
        parser.validate_repo_relative_path(bad)


# ---------------------------------------------------------------------------
# Gate B (verify-vs-spec) acceptance inputs (ADR-008): requirements/design
# ---------------------------------------------------------------------------


def test_gate_b_inputs_default_none_when_absent():
    # A plain one_shot request carries all four Gate B fields as None.
    mapped = parser.parse_ide_request(_request(), ACTOR)
    assert mapped["requirements_md_path"] is None
    assert mapped["design_md_path"] is None
    assert mapped["requirements_md"] is None
    assert mapped["design_md"] is None


def test_gate_b_inputs_default_none_for_spec_without_them():
    mapped = parser.parse_ide_request(
        _spec_request(tasks_md_path=".kiro/specs/x/tasks.md"), ACTOR
    )
    assert mapped["requirements_md_path"] is None
    assert mapped["design_md_path"] is None
    assert mapped["requirements_md"] is None
    assert mapped["design_md"] is None


def test_gate_b_accepts_requirements_and_design_paths():
    mapped = parser.parse_ide_request(
        _spec_request(
            tasks_md_path=".kiro/specs/x/tasks.md",
            requirements_md_path=".kiro/specs/x/requirements.md",
            design_md_path=".kiro/specs/x/design.md",
        ),
        ACTOR,
    )
    assert mapped["requirements_md_path"] == ".kiro/specs/x/requirements.md"
    assert mapped["design_md_path"] == ".kiro/specs/x/design.md"


def test_gate_b_accepts_inline_requirements_and_design():
    mapped = parser.parse_ide_request(
        _spec_request(
            tasks_md_path=".kiro/specs/x/tasks.md",
            requirements_md="# Requirements\n1.1 The system SHALL ...",
            design_md="# Design\nArchitecture ...",
        ),
        ACTOR,
    )
    assert mapped["requirements_md"].startswith("# Requirements")
    assert mapped["design_md"].startswith("# Design")


def test_gate_b_inputs_available_on_one_shot_too():
    mapped = parser.parse_ide_request(
        _request(requirements_md="# Requirements\nacceptance"), ACTOR
    )
    assert mapped["task_type"] == "one_shot"
    assert mapped["requirements_md"].startswith("# Requirements")


@pytest.mark.parametrize("field", ["requirements_md_path", "design_md_path"])
@pytest.mark.parametrize(
    "bad_path", ["../etc/passwd", "/etc/passwd", "a/../b", "-rf", "a b"]
)
def test_gate_b_rejects_traversal_path(field, bad_path):
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(
            _spec_request(tasks_md_path=".kiro/specs/x/tasks.md",
                          **{field: bad_path}),
            ACTOR,
        )


@pytest.mark.parametrize("field", ["requirements_md", "design_md"])
def test_gate_b_rejects_empty_inline(field):
    with pytest.raises(parser.IdeRequestError):
        parser.parse_ide_request(
            _spec_request(tasks_md_path=".kiro/specs/x/tasks.md",
                          **{field: "   "}),
            ACTOR,
        )
