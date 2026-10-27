"""Tests for the hand-rolled stdlib tasks.md parser (FEAT-001).

Covers runtime/tasks_md.py ``parse_tasks_md`` / ``SpecTask`` against realistic
Kiro spec-tasks checklist shapes:

* a multi-task checklist with mixed [ ]/[x], ordering preserved;
* sub-bullets and ``_Requirements:`` lines folded into the owning task body;
* both checkbox-first (``- [ ] 1. T``) and number-first (``1. [ ] T``) orderings;
* a ``# Implementation Plan`` heading and interleaved prose ignored;
* done-state parsed correctly;
* a no-tasks input raising ValueError.

Pure functions, stdlib only; conftest puts runtime/ on sys.path so
``import tasks_md`` resolves directly.
"""

import pytest

import tasks_md  # noqa: E402 - conftest extends sys.path


MULTI_TASK = """\
# Implementation Plan

Some introductory prose that should be ignored.

- [x] 1. Set up the project skeleton
  - create the package layout
  - add the pyproject
  _Requirements: 1.1, 1.2_

- [ ] 2. Build the parser
  - parse the checklist
  _Requirements: 2.3_

- [ ] 3. Wire up the handler
"""


def test_parses_all_tasks_in_order():
    tasks = tasks_md.parse_tasks_md(MULTI_TASK)
    assert [t.id for t in tasks] == ["1", "2", "3"]
    assert [t.title for t in tasks] == [
        "Set up the project skeleton",
        "Build the parser",
        "Wire up the handler",
    ]


def test_done_state_parsed():
    tasks = tasks_md.parse_tasks_md(MULTI_TASK)
    assert tasks[0].done is True
    assert tasks[1].done is False
    assert tasks[2].done is False


def test_subbullets_and_requirements_folded_into_body():
    tasks = tasks_md.parse_tasks_md(MULTI_TASK)
    body1 = tasks[0].body
    assert "create the package layout" in body1
    assert "add the pyproject" in body1
    assert "_Requirements: 1.1, 1.2_" in body1
    # Body does not leak into the next task.
    assert "parse the checklist" not in body1
    assert "_Requirements: 2.3_" in tasks[1].body
    # Last task has no body.
    assert tasks[2].body == ""


def test_uppercase_x_is_done():
    text = "- [X] 1. Done with capital X"
    tasks = tasks_md.parse_tasks_md(text)
    assert tasks[0].done is True


def test_number_first_ordering():
    text = """\
1. [ ] First task
2. [x] Second task
   - a sub bullet
"""
    tasks = tasks_md.parse_tasks_md(text)
    assert [t.id for t in tasks] == ["1", "2"]
    assert tasks[0].done is False
    assert tasks[1].done is True
    assert "a sub bullet" in tasks[1].body


def test_sub_numbered_ids_preserved():
    text = """\
- [ ] 1. Parent
- [ ] 2.1 Child-ish numbering
"""
    tasks = tasks_md.parse_tasks_md(text)
    assert [t.id for t in tasks] == ["1", "2.1"]
    assert tasks[1].title == "Child-ish numbering"


def test_mixed_orderings_in_one_doc():
    text = """\
- [ ] 1. Checkbox first
2. [x] Number first
"""
    tasks = tasks_md.parse_tasks_md(text)
    assert [t.id for t in tasks] == ["1", "2"]
    assert tasks[1].done is True


def test_star_bullet_accepted():
    text = "* [ ] 1. Star bullet task"
    tasks = tasks_md.parse_tasks_md(text)
    assert tasks[0].id == "1"
    assert tasks[0].title == "Star bullet task"


def test_prose_before_first_task_ignored():
    text = """\
# Implementation Plan

This is a paragraph of prose.
Another line.

- [ ] 1. The only task
"""
    tasks = tasks_md.parse_tasks_md(text)
    assert len(tasks) == 1
    assert tasks[0].title == "The only task"
    assert tasks[0].body == ""


def test_empty_input_raises():
    with pytest.raises(ValueError):
        tasks_md.parse_tasks_md("")


def test_whitespace_only_input_raises():
    with pytest.raises(ValueError):
        tasks_md.parse_tasks_md("   \n  \n")


def test_no_tasks_input_raises():
    text = """\
# Implementation Plan

Just prose, no checklist items here.
- a plain bullet that is not a numbered task
"""
    with pytest.raises(ValueError):
        tasks_md.parse_tasks_md(text)
