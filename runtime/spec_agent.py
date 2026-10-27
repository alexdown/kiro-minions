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
                        blocked: dict | None) -> str:
    """Compose the spec-executor PR body (always routed through redact() by the
    caller). Lists the completed tasks as an ordered sequence and, when the job
    stopped early, names the blocked task and WHY (redacted failing output or a
    budget reason).

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

    lines += ["This PR was generated automatically by kiro-minions.", ""]
    return "\n".join(lines)


def _open_spec_pr(repo_dir: Path, base_branch: str, fix_branch: str,
                  summary: str, sonar_hash: str, completed: list[dict],
                  blocked: dict | None, *, draft: bool) -> str:
    """Open ONE spec PR via the same gh-create-via-RUNNER argv-list shape
    agent.step_open_pr uses. The body is built by ``_build_spec_pr_body`` and
    ALWAYS passed through agent.redact() so no secret can leak. No secret is
    ever placed in the gh argv."""
    pr_title = agent.redact(f"spec: {summary or 'spec execution'} [{sonar_hash}]")
    if draft:
        pr_title = f"[DRAFT] {pr_title}"

    pr_body = agent.redact(
        _build_spec_pr_body(summary, sonar_hash, completed, blocked)
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
                  spec_header: str, index: int, total: int,
                  post_install: set[str]) -> tuple[bool, str]:
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
                staging_baseline,
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

        agent.write_phase(sonar_hash, agent.PHASE_OPENING_PR)
        draft = blocked is not None
        pr_url = _open_spec_pr(
            repo_dir, base_branch, fix_branch, summary, sonar_hash,
            completed, blocked, draft=draft,
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
