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
    # Derived fix branch is validated and references the task_id fragment.
    assert mapped["fix_branch"] == f"kiro/ide-{mapped['task_id'][:12]}"


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
    assert mapped["task_id"] == "client-supplied-123"
    # Derived fix branch follows the overridden task_id.
    assert mapped["fix_branch"] == "kiro/ide-client-suppl"


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
