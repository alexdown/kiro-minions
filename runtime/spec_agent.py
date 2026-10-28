"""Spec-executor worker (ADR-002 Option C) for kiro-minions.

Where ``runtime/agent.py`` runs the ONE-SHOT deterministic pipeline (one scoped
instruction -> one fix -> one PR), this module runs the SPEC pipeline: a whole
Kiro ``tasks.md`` (tens of tasks implementing a feature) is offloaded to a
remote AgentCore worker, which reads the WHOLE task list and iterates
task-by-task.

Design (matches ADR-002 Option C and the FEAT-002 plan):

* A NEW deterministic OUTER loop in plain Python — there is NO model reasoning
  in the loop itself. Model reasoning happens ONLY inside each per-task
  ``kiro-cli`` pass, with a FRESH per-task brief (just that task's title/body +
  minimal spec context, NOT the accumulated transcript of previous tasks).
* ONE fix branch for the whole spec; ONE commit per COMPLETED task (each commit
  message names the task); ONE PR per spec.
* STOP-AT-FIRST-BLOCKER: the moment a task cannot be completed (tests still
  failing after its retry budget, a per-task kiro timeout, no committable diff,
  or a job budget breach), the loop STOPS. It never skips ahead to later tasks.
  Prior task commits are kept and pushed, and a DRAFT PR is opened whose body
  lists the completed tasks and names the blocked task + a redacted reason.
* Per-task progress is surfaced as a phase string ``task N/total`` via
  ``agent.write_phase`` so the polling status API can show progress.
* Env-configurable budgets: a per-task retry budget (``SPEC_MAX_TASK_ATTEMPTS``,
  default 3), an overall executed-task cap (``SPEC_MAX_TASKS``, default 50;
  0/unset = unlimited), and an overall wall-clock budget
  (``SPEC_JOB_TIMEOUT_S``, default 3600). The per-task kiro timeout reuses
  ``agent.KIRO_TIMEOUT_S``.

CRITICAL REUSE RULE: this module REUSES ``runtime/agent.py`` building blocks via
``import agent`` and goes through ``agent.RUNNER`` for every subprocess so tests
can inject a FakeRunner. It does NOT create a second subprocess runner and does
NOT duplicate validators beyond the one small defence-in-depth path validator
(mirroring how agent.py duplicates its own validators because the container
cannot import the orchestrator package).

NOTE on the on-disk ``tasks.md`` checkbox: this worker does NOT rewrite the
``[ ]`` -> ``[x]`` checkbox on disk. Progress is recorded by the per-task git
commits (each names its task) and by the PR body, which keeps the scoped-staging
logic clean (a rewritten tasks.md would otherwise be swept into every task's
commit). If checkbox-rewrite becomes desired it can be added as an explicit,
separately-staged step.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import agent  # reuse the one-shot pipeline building blocks (RUNNER indirection)
import review_gates  # shared ADR-008 verification gates (Phase 2, post-loop)
import tasks_md


log = agent.log

# ---------------------------------------------------------------------------
# Spec-executor budgets (read at module level like agent.MAX_FIX_ATTEMPTS /
# KIRO_TIMEOUT_S so they can be tuned via the Runtime env and a module reload in
# tests picks up a monkeypatched value).
# ---------------------------------------------------------------------------

# Per-task retry budget: how many kiro+test attempts a SINGLE task gets before
# it is declared a blocker. Mirrors agent.MAX_FIX_ATTEMPTS semantics.
SPEC_MAX_TASK_ATTEMPTS = int(os.environ.get("SPEC_MAX_TASK_ATTEMPTS", "3"))

# Cap on the number of tasks EXECUTED in one job (already-done tasks are skipped
# and do not count). 0 / unset => unlimited.
SPEC_MAX_TASKS = int(os.environ.get("SPEC_MAX_TASKS", "50"))

# Overall wall-clock budget (seconds) across the whole outer loop. A breach mid
# loop stops the job like a blocker.
SPEC_JOB_TIMEOUT_S = int(os.environ.get("SPEC_JOB_TIMEOUT_S", "3600"))

# Upper bound (bytes) on a single Gate B acceptance document read from the clone
# (requirements.md / design.md, explicit path or sibling default). A file larger
# than this is read only up to the bound (the acceptance text is in any case
# bounded again by agent.truncate_test_output when the Gate B brief is built);
# the cap just stops an accidental huge/binary file from being slurped whole.
SPEC_ACCEPTANCE_MAX_BYTES = int(
    os.environ.get("SPEC_ACCEPTANCE_MAX_BYTES", str(256 * 1024))
)


# ---------------------------------------------------------------------------
# Defence-in-depth path validation.
#
# The tasks_md_path was already traversal-validated at ingress
# (orchestrator/parser.validate_repo_relative_path). This module runs in a
# separate container and cannot import the orchestrator package, so — exactly
# like agent.py duplicates its URL/branch/allowlist validators — we re-validate
# the path here before joining it onto the clone root and reading it.
# ---------------------------------------------------------------------------

_REPO_RELATIVE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")


def _validate_tasks_md_path(path: str, field: str = "tasks_md_path") -> str:
    """Validate a conservative, traversal-safe repo-relative path; raise on any
    violation. Mirrors orchestrator/parser.validate_repo_relative_path."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"field {field} must be a non-empty string")
    value = path.strip()
    if not _REPO_RELATIVE_PATH_RE.match(value):
        raise ValueError(
            f"field {field} must match ^[A-Za-z0-9._/-]{{1,256}}$ "
            "(conservative repo-relative path)"
        )
    if value.startswith("/"):
        raise ValueError(f"field {field} must be relative (no leading '/')")
    if value.startswith("-"):
        raise ValueError(f"field {field} must not start with '-'")
    if value.endswith("/"):
        raise ValueError(f"field {field} must reference a file, not a directory")
    if any(seg == ".." for seg in value.split("/")):
        raise ValueError(f"field {field} must not contain a '..' segment")
    if value == ".":
        raise ValueError(f"field {field} must reference a file, not '.'")
    return value


def _read_tasks_md(payload: dict, repo_dir: Path) -> str:
    """Resolve the tasks.md content for a spec job.

    Inline ``tasks_md`` wins; otherwise ``tasks_md_path`` is re-validated
    (defence in depth) and read from inside the clone, with a final containment
    check that the resolved path really sits under ``repo_dir`` (belt and
    braces against any symlink / normalisation surprise). Raises ValueError when
    neither is present or the file cannot be read.
    """
    inline = payload.get("tasks_md")
    if inline is not None and str(inline).strip():
        return str(inline)

    rel = payload.get("tasks_md_path")
    if rel is None:
        raise ValueError(
            "spec job requires inline 'tasks_md' or a 'tasks_md_path'"
        )
    rel = _validate_tasks_md_path(rel)
    repo_root = repo_dir.resolve()
    target = (repo_root / rel).resolve()
    # Containment: the resolved file must live under the clone root.
    if repo_root != target and repo_root not in target.parents:
        raise ValueError(
            f"tasks_md_path escapes the repository root: {rel!r}"
        )
    if not target.is_file():
        raise ValueError(f"tasks_md_path not found in clone: {rel!r}")
    return target.read_text(errors="replace")


def _resolve_contained_file(repo_dir: Path, rel: str, field: str) -> Path:
    """Re-validate a repo-relative path and resolve it to a clone-contained file.

    Applies the SAME traversal validation (``_validate_tasks_md_path``) and
    clone-root containment check used by ``_read_tasks_md`` so the new Gate B
    spec paths cannot be used to read outside the clone. Raises ValueError on a
    traversal / containment violation. Does NOT require the file to exist (the
    caller decides how to treat a missing optional file).
    """
    rel = _validate_tasks_md_path(rel, field=field)
    repo_root = repo_dir.resolve()
    target = (repo_root / rel).resolve()
    if repo_root != target and repo_root not in target.parents:
        raise ValueError(f"{field} escapes the repository root: {rel!r}")
    return target


def _read_acceptance_doc(target: Path) -> str | None:
    """Safely read a Gate B acceptance document, or return None to skip it.

    Guards the read the same way ``_read_tasks_md`` guards its target:

    * ``target.is_file()`` must hold (a missing / non-regular path — e.g. a
      directory or a dangling optional file — is skipped, never read);
    * the file must be Markdown (``.md``) — the acceptance source is a
      requirements/design doc, so a non-``.md`` contained file is skipped
      rather than read as acceptance text;
    * the read is bounded to ``SPEC_ACCEPTANCE_MAX_BYTES`` so an accidental
      huge/binary file can never be slurped whole into the brief.

    Containment + traversal safety is already enforced by
    ``_resolve_contained_file`` before this runs; this adds the is-file /
    extension / size guards at the read site. Returns the decoded text, or
    ``None`` when the file should be skipped.
    """
    if not target.is_file():
        return None
    if target.suffix.lower() != ".md":
        return None
    with target.open("rb") as handle:
        raw = handle.read(SPEC_ACCEPTANCE_MAX_BYTES)
    return raw.decode("utf-8", errors="replace")


def _read_spec_acceptance(payload: dict, repo_dir: Path,
                          tasks_md_path: str | None) -> str | None:
    """Resolve the Gate B acceptance text (requirements.md / design.md content).

    Precedence (ADR-008 Q3):

    1. INLINE ``payload['requirements_md']`` / ``payload['design_md']`` win.
    2. Else ``payload['requirements_md_path']`` / ``payload['design_md_path']``
       (re-validated with ``_validate_tasks_md_path`` + the same clone-root
       containment check as ``tasks_md_path``), read from inside the clone.
    3. Else the ``.kiro/specs/<feature>/`` SIBLING default: when
       ``tasks_md_path`` is ``.kiro/specs/<feature>/tasks.md`` look for
       ``requirements.md`` / ``design.md`` as siblings next to it.

    Each resolved source is concatenated with a labelled section header. When
    NOTHING is found, returns None so Gate B is SKIPPED-with-notice (never
    blocks). Applies the SAME traversal + containment safety as the tasks path;
    never reads outside the clone root.
    """
    sections: list[tuple[str, str]] = []

    def _add(label: str, text: str | None) -> bool:
        if text is not None and str(text).strip():
            sections.append((label, str(text)))
            return True
        return False

    # (1) Inline wins, per-doc and independently.
    have_requirements = _add("requirements.md", payload.get("requirements_md"))
    have_design = _add("design.md", payload.get("design_md"))

    # (2) Explicit paths (re-validated + clone-contained) fill any doc not
    # already supplied inline.
    if not have_requirements:
        rel = payload.get("requirements_md_path")
        if rel is not None:
            target = _resolve_contained_file(
                repo_dir, rel, field="requirements_md_path",
            )
            have_requirements = _add(
                "requirements.md", _read_acceptance_doc(target),
            )
    if not have_design:
        rel = payload.get("design_md_path")
        if rel is not None:
            target = _resolve_contained_file(
                repo_dir, rel, field="design_md_path",
            )
            have_design = _add(
                "design.md", _read_acceptance_doc(target),
            )

    # (3) .kiro/specs/<feature>/ sibling default for any doc still missing: only
    # when the tasks path is itself under .kiro/specs/<feature>/tasks.md.
    if (not have_requirements or not have_design) and tasks_md_path:
        try:
            tasks_target = _resolve_contained_file(
                repo_dir, tasks_md_path, field="tasks_md_path",
            )
        except ValueError:
            tasks_target = None
        if tasks_target is not None:
            parent = tasks_target.parent
            # Confirm the shape .kiro/specs/<feature>/tasks.md.
            if (tasks_target.name == "tasks.md"
                    and parent.parent.name == "specs"
                    and parent.parent.parent.name == ".kiro"):
                if not have_requirements:
                    sib = parent / "requirements.md"
                    have_requirements = _add(
                        "requirements.md", _read_acceptance_doc(sib),
                    )
                if not have_design:
                    sib = parent / "design.md"
                    have_design = _add(
                        "design.md", _read_acceptance_doc(sib),
                    )

    if not sections:
        return None
    return "\n\n".join(
        f"=== {label} ===\n{text}" for label, text in sections
    )


def _build_spec_task_brief(task: tasks_md.SpecTask, summary: str,
                           spec_header: str, index: int, total: int) -> str:
    """Compose the FRESH per-task kiro brief (delivered via STDIN, never argv).

    The brief contains ONLY this task's title/body plus minimal spec context
    (the job summary and a short slice of the tasks.md header) — NOT the
    accumulated transcript of previously-completed tasks. Routed through
    ``agent.redact`` by the caller before it reaches kiro-cli.
    """
    parts: list[str] = []
    if summary:
        parts.append(f"You are implementing a spec: {summary}\n")
    if spec_header:
        parts.append(
            "Spec context (for orientation only — implement ONLY the task "
            f"below):\n{spec_header}\n\n"
        )
    parts.append(
        f"Implement task {index}/{total} ({task.id}): {task.title}\n"
    )
    if task.body:
        parts.append(f"\n{task.body}\n")
    parts.append(
        "\nImplement ONLY this task. Make the repository's tests pass. Do not "
        "start later tasks.\n"
    )
    return "".join(parts)


def _spec_header_slice(tasks_md_text: str, budget: int = 500) -> str:
    """Return a short leading slice of the tasks.md (heading/prose before the
    first task) to orient kiro, bounded to ``budget`` chars."""
    if not tasks_md_text:
        return ""
    head = tasks_md_text.strip().splitlines()
    collected: list[str] = []
    for line in head:
        # Stop at the first checklist line; the header is the prose above it.
        if re.match(r"^\s{0,3}(?:[-*+]\s+\[[ xX]\]|\d+(?:\.\d+)*\.?\s+\[[ xX]\])",
                    line):
            break
        collected.append(line)
    text = "\n".join(collected).strip()
    if len(text) > budget:
        text = text[:budget].rstrip() + "…"
    return text


def _run_task_kiro(repo_dir: Path, brief: str) -> None:
    """Run ONE per-task kiro-cli pass, mirroring agent.step_kiro_fix exactly.

    The brief is delivered on STDIN (``input=``), never argv; the argv stays a
    LIST (shell=False); the timeout is agent.KIRO_TIMEOUT_S; the brief is routed
    through agent.redact() so no secret can reach kiro-cli via stdin either.
    A subprocess.TimeoutExpired propagates to the caller, which treats it as the
    task's failure for that attempt.
    """
    agent._run(
        ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools"],
        cwd=repo_dir,
        timeout=agent.KIRO_TIMEOUT_S,
        input=agent.redact(brief),
    )


def _build_spec_pr_body(summary: str, sonar_hash: str, completed: list[dict],
                        blocked: dict | None,
                        verification_section: str | None = None) -> str:
    """Compose the spec-executor PR body (always routed through redact() by the
    caller). Lists the completed tasks as an ordered sequence and, when the job
    stopped early, names the blocked task and WHY (redacted failing output or a
    budget reason).

    When ``verification_section`` is provided (the ADR-008 Phase-2 gates ran) it
    is appended as a ``## Verification`` block that already names any gate that
    downgraded the PR to a draft + its findings (rendered by
    ``review_gates.render_verification_section``). It is ``None`` whenever the
    gates did not run (flags off, or a Phase-1 blocker short-circuited Phase 2),
    keeping the happy/blocker body byte-for-byte unchanged.

    This is deliberately NOT ``agent._build_pr_body`` (which is SonarQube-shaped
    and single-fix); a spec body is a task list.
    """
    lines = [
        "## Automated spec execution",
        "",
        f"**Spec:** {summary or '(unnamed spec)'}",
        f"**task_id:** `{sonar_hash}`",
        "",
    ]
    if completed:
        lines += ["### Completed tasks", ""]
        for i, task in enumerate(completed, start=1):
            lines.append(f"{i}. `{task['id']}` {task['title']}")
        lines.append("")
    else:
        lines += ["_No tasks were completed before the job stopped._", ""]

    if blocked is not None:
        lines += [
            "> :warning: **STOPPED AT FIRST BLOCKER — HUMAN REVIEW REQUIRED**",
            "",
            f"**Blocked task:** `{blocked['id']}` {blocked['title']}",
            f"**Reason:** {blocked['reason']}",
            "",
            "The loop stopped at the task above and did NOT attempt any later "
            "tasks. The completed tasks above are included as individual "
            "commits for review.",
            "",
        ]
        failing = blocked.get("output")
        if failing:
            lines += [
                "<details><summary>Truncated failing output</summary>",
                "",
                "```",
                failing.strip(),
                "```",
                "",
                "</details>",
                "",
            ]
    else:
        lines += ["All tasks in the spec completed successfully.", ""]

    if verification_section:
        lines += [verification_section.strip(), ""]

    lines += ["This PR was generated automatically by kiro-minions.", ""]
    return "\n".join(lines)


def _open_spec_pr(repo_dir: Path, base_branch: str, fix_branch: str,
                  summary: str, sonar_hash: str, completed: list[dict],
                  blocked: dict | None, *, draft: bool,
                  verification_section: str | None = None) -> str:
    """Open ONE spec PR via the same gh-create-via-RUNNER argv-list shape
    agent.step_open_pr uses. The body is built by ``_build_spec_pr_body`` and
    ALWAYS passed through agent.redact() so no secret can leak. No secret is
    ever placed in the gh argv. ``verification_section`` (ADR-008 Phase 2) is
    threaded into the body when the gates ran; ``None`` leaves the body
    unchanged."""
    pr_title = agent.redact(f"spec: {summary or 'spec execution'} [{sonar_hash}]")
    if draft:
        pr_title = f"[DRAFT] {pr_title}"

    pr_body = agent.redact(
        _build_spec_pr_body(summary, sonar_hash, completed, blocked,
                            verification_section)
    )

    argv = [
        "gh", "pr", "create",
        "--base", base_branch,
        "--head", fix_branch,
        "--title", pr_title,
        "--body", pr_body,
    ]
    if draft:
        argv.append("--draft")

    result = agent._run(argv, cwd=repo_dir)
    pr_url = result.stdout.strip()
    log.info("Spec PR opened (draft=%s): %s", draft, pr_url)
    return pr_url


def _execute_task(repo_dir: Path, task: tasks_md.SpecTask, summary: str,
                  spec_header: str, index: int, total: int) -> tuple[bool, str]:
    """Run the FRESH per-task kiro + test + retry cycle for ONE task.

    Returns ``(passed, last_output)``. Applies the per-task retry budget
    (SPEC_MAX_TASK_ATTEMPTS) with the reset-between-attempts behaviour
    (agent._reset_worktree_for_retry) and early-stop on a repeated failure
    fingerprint — mirroring the one-shot loop but scoped to a single task. A
    per-task kiro timeout is caught and counted as that attempt's failure (it
    does NOT abort the whole job).
    """
    last_failure: str | None = None
    previous_diff: str | None = None
    previous_fingerprint: str | None = None
    passed = False
    output = ""

    for attempt in range(1, SPEC_MAX_TASK_ATTEMPTS + 1):
        log.info("=== task %s: kiro-cli (attempt %d/%d) ===",
                 task.id, attempt, SPEC_MAX_TASK_ATTEMPTS)
        brief = _build_spec_task_brief(task, summary, spec_header, index, total)
        if attempt > 1 and last_failure:
            brief = (
                "PREVIOUS ATTEMPT FAILED. Test output:\n"
                f"{agent.truncate_test_output(last_failure)}\n\n"
                + (
                    "The previous attempt made these changes (which did NOT "
                    f"fix it):\n{agent.truncate_test_output(previous_diff)}\n\n"
                    if previous_diff else ""
                )
                + brief
            )
        try:
            _run_task_kiro(repo_dir, brief)
        except subprocess.TimeoutExpired:
            timeout_msg = agent.redact(
                f"kiro-cli timed out after {agent.KIRO_TIMEOUT_S} seconds"
            )
            log.warning("task %s attempt %d: %s", task.id, attempt, timeout_msg)
            last_failure = timeout_msg
            output = timeout_msg
            passed = False
            if attempt < SPEC_MAX_TASK_ATTEMPTS:
                agent._reset_worktree_for_retry(repo_dir)
            previous_diff = None
            previous_fingerprint = None
            continue

        passed, output = agent.step_run_tests(repo_dir)
        if passed:
            break

        last_failure = output
        fingerprint = agent._fingerprint_failure(output)
        if previous_fingerprint is not None and fingerprint == previous_fingerprint:
            log.warning(
                "task %s: repeated failure fingerprint; stopping task early.",
                task.id,
            )
            break
        previous_fingerprint = fingerprint

        if attempt < SPEC_MAX_TASK_ATTEMPTS:
            diff_result = agent._run(["git", "diff"], cwd=repo_dir, check=False)
            previous_diff = (diff_result.stdout or "").strip() or None
            agent._reset_worktree_for_retry(repo_dir)

    return passed, output


def run_spec_pipeline(payload: dict) -> dict:
    """Execute the whole-spec pipeline deterministically. Returns a result dict.

    The outer loop is plain Python (no model reasoning); each task gets a fresh
    per-task kiro pass. Stops at the first blocker, keeps prior commits, opens
    ONE PR (draft on a blocker/partial job, non-draft when everything passed).
    """
    repo_clone_url = payload["repo_clone_url"]
    base_branch = payload.get("base_branch", "main")
    sonar_hash = payload["sonar_hash"]
    fix_branch = (
        payload.get("fix_branch") or f"kiro-minions/spec-{sonar_hash[:12]}"
    )
    summary = payload.get("summary", "spec execution")

    # Defence in depth (#4): validate every value that flows into git commands
    # BEFORE any subprocess runs; a ValueError here fails safely pre-clone.
    agent._validate_clone_url(repo_clone_url)
    agent._validate_branch_name(base_branch, field="base_branch")
    agent._validate_branch_name(fix_branch, field="fix_branch")
    agent._validate_repo_allowed(repo_clone_url)

    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="kiro-minions-spec-") as workdir:
        workdir = Path(workdir)

        log.info("=== SPEC STEP 0: Configure git auth ===")
        agent.setup_git_auth()

        log.info("=== SPEC STEP 1: Clone ===")
        agent.write_phase(sonar_hash, agent.PHASE_CLONING)
        repo_dir = agent.step_clone(repo_clone_url, base_branch, workdir)

        log.info("=== SPEC STEP 2: Create fix branch ===")
        agent.step_create_branch(repo_dir, fix_branch)

        # Resolve + parse the task list (defence-in-depth re-validation inside).
        tasks_md_text = _read_tasks_md(payload, repo_dir)
        spec_tasks = tasks_md.parse_tasks_md(tasks_md_text)
        spec_header = _spec_header_slice(tasks_md_text)
        total = len(spec_tasks)

        # Install deps ONCE after clone (like the one-shot path); the post
        # install porcelain snapshot is the running staging baseline so install
        # churn is never attributed to a task.
        log.info("=== SPEC STEP 2b: Install dependencies ===")
        agent.step_install_deps(repo_dir)
        staging_baseline = agent._git_status_porcelain(repo_dir)

        agent.write_phase(sonar_hash, agent.PHASE_FIXING)

        completed: list[dict] = []
        blocked: dict | None = None
        executed = 0

        for index, task in enumerate(spec_tasks, start=1):
            if task.done:
                log.info("task %s already [x] — skipping.", task.id)
                continue

            # Budget checks BEFORE starting the task (treat a breach as a
            # blocker: stop, keep prior commits, draft PR).
            elapsed = time.monotonic() - started
            if SPEC_JOB_TIMEOUT_S and elapsed > SPEC_JOB_TIMEOUT_S:
                blocked = {
                    "id": task.id,
                    "title": task.title,
                    "reason": agent.redact(
                        f"job wall-clock budget exceeded "
                        f"({int(elapsed)}s > SPEC_JOB_TIMEOUT_S={SPEC_JOB_TIMEOUT_S}s)"
                    ),
                    "output": None,
                }
                break
            if SPEC_MAX_TASKS and executed >= SPEC_MAX_TASKS:
                blocked = {
                    "id": task.id,
                    "title": task.title,
                    "reason": agent.redact(
                        f"executed-task budget exceeded "
                        f"(SPEC_MAX_TASKS={SPEC_MAX_TASKS})"
                    ),
                    "output": None,
                }
                break

            # Per-task progress phase (status API surfaces this).
            agent.write_phase(sonar_hash, f"task {index}/{total}")

            executed += 1
            passed, output = _execute_task(
                repo_dir, task, summary, spec_header, index, total,
            )

            if not passed:
                blocked = {
                    "id": task.id,
                    "title": task.title,
                    "reason": agent.redact(
                        "tests still failing after the per-task retry budget"
                    ),
                    "output": agent.redact(agent.truncate_test_output(output)),
                }
                break

            # Commit ONLY this task's diff. The per-task title is the summary and
            # the logical task_id goes in both the ticket_id and sonar_hash
            # positions so the commit message names the task.
            # Redact the commit summary: the task title is task-derived free
            # text and a secret in it must never reach the commit message argv.
            commit_summary = agent.redact(f"{task.id} {task.title}")
            had_changes, _staged = agent.step_commit(
                repo_dir, commit_summary, sonar_hash, sonar_hash,
                post_install=staging_baseline,
            )
            if not had_changes:
                # kiro produced no committable diff for a task whose tests pass:
                # treat as a blocker (nothing to show for the task).
                blocked = {
                    "id": task.id,
                    "title": task.title,
                    "reason": agent.redact(
                        "kiro-cli produced no committable diff for this task"
                    ),
                    "output": None,
                }
                break

            completed.append({"id": task.id, "title": task.title})
            # Advance the staging baseline so the NEXT task's commit captures
            # only its own diff.
            staging_baseline = agent._git_status_porcelain(repo_dir)

        # Nothing committed and a blocker with no prior work => no empty PR.
        if not completed and blocked is not None:
            reason = blocked["reason"]
            return {
                "status": agent.STATUS_TESTS_FAILED,
                "reason": reason,
                "pr_url": "",
                "draft": True,
                "tasks": {
                    "total": total,
                    "completed": [],
                    "blocked": {"id": blocked["id"], "reason": reason},
                },
                "elapsed": int(time.monotonic() - started),
            }

        # Push whatever task commits exist, then open ONE PR.
        log.info("=== SPEC STEP: Push ===")
        agent.step_push(repo_dir, fix_branch)

        # -----------------------------------------------------------------
        # PHASE 2 — VERIFY GATES (ADR-008, post-loop, runs ONCE).
        #
        # Runs ONLY when Phase 1 completed ALL tasks (blocked is None) and there
        # is a cumulative change to verify. A Phase-1 blocker means there is no
        # complete change to review, so the gates DO NOT run and today's
        # blocker/draft-PR behaviour is preserved exactly. With both gate flags
        # OFF review_gates.run_gates skips both gates, gate_driven_draft stays
        # False, and verification_section stays None, so the happy/blocker body
        # and the non-draft/draft decision below are byte-for-byte unchanged.
        # -----------------------------------------------------------------
        gate_driven_draft = False
        gate_failed_labels: list[str] = []
        verification_section: str | None = None
        if blocked is None and completed:
            tasks_md_path = payload.get("tasks_md_path")
            diff_result = agent._run(
                ["git", "diff", f"{base_branch}..{fix_branch}"],
                cwd=repo_dir, check=False,
            )
            diff = diff_result.stdout or ""
            if diff.strip():
                # Gate A intent: the job summary + a bounded tasks.md header
                # slice already in scope (fresh-context reviewer orientation).
                intent_parts = []
                if summary:
                    intent_parts.append(summary)
                if spec_header:
                    intent_parts.append(spec_header)
                intent = "\n\n".join(intent_parts)

                acceptance_text = _read_spec_acceptance(
                    payload, repo_dir, tasks_md_path,
                )

                # Write the gate phases (ADR-003) BEFORE running each gate, AFTER
                # the per-task 'task N/total' phases.
                agent.write_phase(sonar_hash, "reviewing")
                phase_result = review_gates.run_gates(
                    repo_dir, diff, intent, acceptance_text,
                )
                # Gate B is the verify-vs-spec pass; surface its phase too. Both
                # gates already ran inside run_gates (bounded, no loop); the
                # second write_phase simply advances the status string so a
                # polling client sees the verify stage.
                agent.write_phase(sonar_hash, "verifying-spec")

                if phase_result.any_gate_ran:
                    gate_driven_draft = phase_result.should_draft
                    gate_failed_labels = list(phase_result.failed_gate_labels)
                    verification_section = phase_result.verification_section

        agent.write_phase(sonar_hash, agent.PHASE_OPENING_PR)
        # Draft drivers: a Phase-1 blocker (as today) OR a Phase-2 gate
        # downgrade. When blocked is None the ONLY draft driver is the gates.
        draft = blocked is not None or gate_driven_draft
        pr_url = _open_spec_pr(
            repo_dir, base_branch, fix_branch, summary, sonar_hash,
            completed, blocked, draft=draft,
            verification_section=verification_section,
        )

    elapsed_total = int(time.monotonic() - started)
    tasks_summary = {
        "total": total,
        "completed": [t["id"] for t in completed],
        "blocked": (
            {"id": blocked["id"], "reason": blocked["reason"]}
            if blocked is not None else None
        ),
    }

    if blocked is not None:
        return {
            "status": agent.STATUS_TESTS_FAILED,
            "reason": blocked["reason"],
            "pr_url": pr_url,
            "draft": True,
            "tasks": tasks_summary,
            "elapsed": elapsed_total,
        }

    # A gate-driven draft (no Phase-1 blocker): commits are kept/pushed (ADR-008
    # Q4), the PR is a DRAFT, and the status maps to the existing
    # RETRYABLE-ish STATUS_TESTS_FAILED so the dispatch layer surfaces a
    # "needs-human" terminal state (mirroring the blocker mapping) rather than a
    # clean success. A redacted reason names the failed gate(s). The flags-OFF
    # path never reaches this branch (gate_driven_draft stays False), so the
    # existing non-draft success result shape is unchanged.
    if gate_driven_draft:
        return {
            "status": agent.STATUS_TESTS_FAILED,
            "reason": agent.redact(
                "verification gate(s) flagged the change: "
                + ", ".join(gate_failed_labels)
            ),
            "pr_url": pr_url,
            "draft": True,
            "tasks": tasks_summary,
            "elapsed": elapsed_total,
        }

    return {
        "status": "success",
        "pr_url": pr_url,
        "draft": False,
        "tasks": tasks_summary,
        "elapsed": elapsed_total,
    }


# ---------------------------------------------------------------------------
# Entrypoint routing helper.
#
# agent.entrypoint branches EARLY on payload['task_type']: when 'spec' it calls
# run_spec_dispatch (below) which runs the blocking loop off the event loop via
# asyncio.to_thread and writes the terminal DynamoDB status/phase keyed on
# sonar_hash, mirroring the one-shot entrypoint's status mapping. The default /
# absent task_type keeps the one-shot path byte-for-byte unchanged.
# ---------------------------------------------------------------------------


async def run_spec_dispatch(payload: dict):
    """Run the spec pipeline off the event loop and write terminal status.

    Yields the same stream-event shape the one-shot entrypoint yields. The
    blocking pipeline runs via asyncio.to_thread (#14) so /ping stays
    responsive. Terminal status is keyed on sonar_hash:
      success                -> done + pr_url
      blocked/partial        -> tests_failed + redacted reason  (RETRYABLE)
      exception              -> failed + redacted reason
    """
    sonar_hash = payload.get("sonar_hash")

    yield {"event": {"text": agent.redact(
        f"Starting spec execution for {payload.get('sonar_hash', 'unknown')}...\n"
    )}}

    try:
        result = await asyncio.to_thread(run_spec_pipeline, payload)
    except Exception as exc:  # noqa: BLE001 - map any failure to a terminal status
        log.exception("Spec pipeline failed")
        reason = agent.redact(str(exc))
        result = {"status": "failed", "reason": reason}
        agent.write_job_status(
            sonar_hash, agent.STATUS_FAILED,
            {"reason": reason,
             "phase": agent.PHASE_FOR_STATUS[agent.STATUS_FAILED]},
        )
    else:
        if result.get("status") == "success":
            agent.write_job_status(
                sonar_hash, agent.STATUS_DONE,
                {"pr_url": result.get("pr_url", ""),
                 "phase": agent.PHASE_FOR_STATUS[agent.STATUS_DONE]},
            )
        else:
            agent.write_job_status(
                sonar_hash, agent.STATUS_TESTS_FAILED,
                {"reason": agent.redact(str(result.get("reason", ""))),
                 "phase": agent.PHASE_FOR_STATUS[agent.STATUS_TESTS_FAILED]},
            )

    yield {"event": {"text": agent.redact(json.dumps(result, indent=2))}}

    log.info("Spec pipeline result: %s", agent.redact(json.dumps(result)))
