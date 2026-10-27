"""Hand-rolled, pure-stdlib parser for the Kiro spec-tasks ``tasks.md`` format.

The spec-executor worker (ADR-002 Option C) offloads a WHOLE ``tasks.md`` (a
markdown checklist of tasks produced by a local spec-driven workflow) to a
remote AgentCore worker. That worker needs to read the whole list and iterate
task-by-task. This module turns the markdown checklist into an ordered list of
:class:`SpecTask` items.

Design constraints (see ``.agents/tasks/.../context.json``):

* **Pure stdlib only** (``re`` / ``dataclasses``). The parser MUST NOT add a new
  runtime dependency.
* Tolerant of real Kiro ``tasks.md`` shapes: a leading ``# Implementation Plan``
  heading, blank lines, and prose between tasks are ignored.
* Tolerant of both checkbox-first (``- [ ] 1. Title``) and number-first
  (``1. [ ] Title``) orderings, and of nested indentation.
* Sub-bullets and ``_Requirements: x.y_`` lines nested under a top-level task are
  folded into that task's ``body`` (preserved as text), NOT emitted as separate
  task items.
* Raises :class:`ValueError` (never crashes) on input with no parseable tasks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["SpecTask", "parse_tasks_md"]


# A top-level task line, tolerant of two orderings:
#   checkbox-first : - [ ] 1. Title      /  * [x] 2.1 Title
#   number-first   : 1. [ ] Title        /  2.1 [x] Title
#
# Group 'done' captures the checkbox contents (space / x / X); group 'num'
# captures the task number (e.g. '1', '2.1'); group 'title' captures the rest.
#
# A leading indentation of up to 1 level (0-3 spaces) is treated as top-level;
# anything more deeply indented is body of the preceding task.
_CHECKBOX = r"\[(?P<done>[ xX])\]"
_NUMBER = r"(?P<num>\d+(?:\.\d+)*)\.?"

_TASK_CHECKBOX_FIRST = re.compile(
    r"^(?P<indent>\s{0,3})[-*+]\s+" + _CHECKBOX + r"\s+" + _NUMBER
    + r"\s+(?P<title>.*\S)\s*$"
)
_TASK_NUMBER_FIRST = re.compile(
    r"^(?P<indent>\s{0,3})" + _NUMBER + r"\s+" + _CHECKBOX
    + r"\s+(?P<title>.*\S)\s*$"
)


@dataclass
class SpecTask:
    """One top-level task parsed from a ``tasks.md`` checklist.

    Attributes:
        id: the task number as a string (e.g. ``"1"`` or ``"2.1"`` when
            sub-numbered).
        title: the task text after the number.
        done: ``True`` for ``[x]`` / ``[X]``; ``False`` for ``[ ]``.
        body: the indented sub-bullet lines and any ``_Requirements: x.y_`` refs
            that belong to this task, preserved verbatim (joined with newlines,
            trailing blank lines stripped). Empty string when the task has no
            body.
    """

    id: str
    title: str
    done: bool
    body: str = field(default="")


def _match_task(line: str):
    """Return a regex match for a top-level task line, or ``None``.

    Tries the checkbox-first ordering, then the number-first ordering.
    """
    return _TASK_CHECKBOX_FIRST.match(line) or _TASK_NUMBER_FIRST.match(line)


def parse_tasks_md(text: str) -> list[SpecTask]:
    """Parse a Kiro spec-tasks ``tasks.md`` into an ordered list of tasks.

    A task item is a top-level numbered checklist entry. Lines nested under a
    task (sub-bullets, ``_Requirements:`` refs, continuation prose) are folded
    into that task's ``body`` preserving order and text. A leading
    ``# Implementation Plan`` heading, blank lines, and prose that appears
    before the first task are ignored.

    Args:
        text: the full ``tasks.md`` content.

    Returns:
        An ordered list of :class:`SpecTask`, in document order.

    Raises:
        ValueError: when ``text`` is empty/``None`` or contains no parseable
            task items.
    """
    if not text or not text.strip():
        raise ValueError("tasks.md is empty: no parseable task items")

    tasks: list[SpecTask] = []
    body_lines: list[str] = []

    def _flush_body() -> None:
        # Attach the accumulated body to the most recent task, trimming
        # trailing blank lines but preserving internal structure/indentation.
        if not tasks:
            body_lines.clear()
            return
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        tasks[-1].body = "\n".join(body_lines)
        body_lines.clear()

    for raw_line in text.splitlines():
        match = _match_task(raw_line)
        if match:
            # Close out the previous task's body before starting a new one.
            _flush_body()
            done = match.group("done") in ("x", "X")
            tasks.append(
                SpecTask(
                    id=match.group("num"),
                    title=match.group("title").strip(),
                    done=done,
                )
            )
            continue

        # Not a task line. If we are inside a task, this line is body (keep it
        # verbatim, including indentation). Before the first task it is prose /
        # a heading and is ignored.
        if tasks:
            body_lines.append(raw_line.rstrip("\n"))

    _flush_body()

    if not tasks:
        raise ValueError(
            "tasks.md contains no parseable task items "
            "(expected a numbered checklist like '- [ ] 1. Title')"
        )

    return tasks
