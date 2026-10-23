"""B1 deterministic workflow agent for kiro-minions.

This is NOT a model-in-a-loop. It is a scripted pipeline that receives a
TicketPayload from the orchestrator and executes a fixed sequence:

    clone → branch → kiro-cli fix → test → retry loop → commit → push → PR

The only "intelligence" is inside kiro-cli itself. Everything else is
deterministic shell commands with structured error handling.

Runs as an AgentCore Runtime container. Exposes /invocations (POST) and
/ping (GET) per the Runtime contract.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_FIX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "3"))

# Names of the environment variables whose VALUES must never leak into argv,
# logs, exceptions, or responses. redact() reads them from os.environ at call
# time (not cached) so tests can monkeypatch the environment without a reload.
_SECRET_ENV_VARS = ("GITHUB_TOKEN", "KIRO_API_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("kiro-minions-agent")

app = BedrockAgentCoreApp()


# ---------------------------------------------------------------------------
# Validation (issue #4: shell-injection defence in depth)
#
# These patterns DUPLICATE orchestrator/parser.py on purpose: this module runs
# in a separate AgentCore Runtime container and cannot import the orchestrator
# package. Re-validating here means a crafted payload that somehow bypassed the
# orchestrator still cannot reach a shell command.
# ---------------------------------------------------------------------------

_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_CLONE_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?:\.git)?$"
)


def _validate_branch_name(name: str, field: str = "branch") -> str:
    """Validate a branch name; raise ValueError on any violation."""
    if name is None or not _BRANCH_RE.match(name):
        raise ValueError(
            f"invalid {field}: {name!r} does not match ^[A-Za-z0-9._/-]{{1,200}}$"
        )
    if name.startswith("-"):
        raise ValueError(f"invalid {field}: {name!r} must not start with '-'")
    if ".." in name:
        raise ValueError(f"invalid {field}: {name!r} must not contain '..'")
    return name


def _validate_clone_url(url: str, field: str = "repo_clone_url") -> str:
    """Validate a GitHub HTTPS clone URL; raise ValueError on any violation."""
    if url is None or not _CLONE_URL_RE.match(url):
        raise ValueError(
            f"invalid {field}: {url!r} is not a https://github.com/<owner>/<repo>"
            " clone URL"
        )
    return url


# ---------------------------------------------------------------------------
# Secret redaction (issue #3)
# ---------------------------------------------------------------------------

REDACTION_MASK = "***REDACTED***"


def redact(text: str | None) -> str | None:
    """Mask secret values (GITHUB_TOKEN, KIRO_API_KEY) wherever they appear.

    Reads the secret values from ``os.environ`` at call time so the result is
    never cached at import and tests can set/unset the env freely. Empty or
    unset env values are skipped (we never mask an empty string). ``None`` input
    is returned unchanged so callers can pass through optional values safely.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if value:  # skip empty/unset: never mask the empty string
            text = text.replace(value, REDACTION_MASK)
    return text


# ---------------------------------------------------------------------------
# Git authentication (issue #3: no token in argv)
# ---------------------------------------------------------------------------


def setup_git_auth() -> None:
    """Configure git to authenticate via gh, without the token in any argv.

    When GITHUB_TOKEN is set, ``gh auth setup-git`` registers gh as git's
    credential helper. gh reads the token from the GITHUB_TOKEN/GH_TOKEN
    environment variable itself, so the token value is never passed as a
    command-line argument (and never logged, since redact() masks it anyway).
    """
    if not os.environ.get("GITHUB_TOKEN"):
        log.info("GITHUB_TOKEN not set — skipping gh auth setup-git.")
        return
    _run(["gh", "auth", "setup-git"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: str | list[str], cwd: str | Path | None = None, check: bool = True,
         timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a shell command, log it, and return the result.

    All displayed/logged text (the command line, STDOUT, STDERR) and the
    RuntimeError raised on non-zero exit are passed through redact() so secret
    values can never leak into logs or exception messages (issue #3).
    """
    if isinstance(cmd, str):
        shell = True
        display = cmd
    else:
        shell = False
        display = " ".join(cmd)

    log.info("RUN: %s (cwd=%s)", redact(display), cwd or ".")
    result = subprocess.run(
        cmd, shell=shell, cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    if result.stdout:
        log.info("STDOUT:\n%s", redact(result.stdout[-2000:]))
    if result.stderr:
        log.info("STDERR:\n%s", redact(result.stderr[-2000:]))
    if check and result.returncode != 0:
        raise RuntimeError(redact(
            f"Command failed (rc={result.returncode}): {display}\n"
            f"stderr: {result.stderr[-500:]}"
        ))
    return result


def _detect_test_command(repo_dir: Path) -> str | None:
    """Auto-detect the test runner for the repo."""
    if (repo_dir / "package.json").exists():
        return "npm test"
    if (repo_dir / "pom.xml").exists():
        return "mvn test"
    if (repo_dir / "Makefile").exists():
        makefile = (repo_dir / "Makefile").read_text(errors="replace")
        if "test:" in makefile or "test :" in makefile:
            return "make test"
    # Python: pytest.ini, pyproject.toml with [tool.pytest], setup.py, or tests/ dir
    if (repo_dir / "pytest.ini").exists():
        return "pytest"
    if (repo_dir / "pyproject.toml").exists():
        content = (repo_dir / "pyproject.toml").read_text(errors="replace")
        if "pytest" in content or "tool.pytest" in content:
            return "pytest"
    if (repo_dir / "setup.py").exists() and (repo_dir / "tests").is_dir():
        return "pytest"
    if (repo_dir / "tests").is_dir():
        return "pytest"
    return None


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


def step_clone(clone_url: str, base_branch: str, workdir: Path) -> Path:
    """Clone the repo and checkout the base branch.

    The plain validated https URL is used directly — no token is injected into
    the URL (issue #3). Authentication is handled by git's gh credential helper
    configured in setup_git_auth().
    """
    repo_name = clone_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    repo_dir = workdir / repo_name

    _run([
        "git", "clone", "--branch", base_branch, "--single-branch",
        clone_url, str(repo_dir),
    ])
    _run(
        ["git", "config", "user.email",
         "kiro-minions[bot]@users.noreply.github.com"],
        cwd=repo_dir,
    )
    _run(["git", "config", "user.name", "kiro-minions[bot]"], cwd=repo_dir)
    return repo_dir


def step_create_branch(repo_dir: Path, fix_branch: str) -> None:
    """Create and checkout the fix branch."""
    _run(["git", "checkout", "-b", fix_branch], cwd=repo_dir)


def step_kiro_fix(repo_dir: Path, description: str, file_to_fix: str | None,
                  attempt: int, last_failure: str | None) -> None:
    """Run kiro-cli headless to apply the fix."""
    # Build the brief: on first attempt, just the issue. On retries, add failure context.
    brief_parts = []
    if attempt > 1 and last_failure:
        brief_parts.append(
            f"PREVIOUS ATTEMPT FAILED. Test output:\n{last_failure}\n\n"
            "Please fix the issue considering the test failure above.\n\n"
        )
    brief_parts.append(description)
    if file_to_fix:
        brief_parts.append(f"\n\nFocus on file: {file_to_fix}")

    brief = "".join(brief_parts)

    _run(
        ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools", brief],
        cwd=repo_dir,
        timeout=300,
    )


def step_run_tests(repo_dir: Path) -> tuple[bool, str]:
    """Detect and run the test suite. Returns (passed, output)."""
    test_cmd = _detect_test_command(repo_dir)
    if test_cmd is None:
        log.info("No test suite detected — skipping tests.")
        return True, "no tests detected"

    log.info("Detected test command: %s", test_cmd)
    result = _run(test_cmd, cwd=repo_dir, check=False, timeout=300)
    output = (result.stdout + "\n" + result.stderr).strip()
    passed = result.returncode == 0
    if passed:
        log.info("Tests PASSED.")
    else:
        log.warning("Tests FAILED (rc=%d).", result.returncode)
    return passed, output


def step_commit(repo_dir: Path, summary: str, ticket_id: str, sonar_hash: str) -> bool:
    """Stage all changes and commit. Returns True if there were changes to commit."""
    _run(["git", "add", "-A"], cwd=repo_dir)

    # Check if there's anything to commit
    result = _run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir, check=False)
    if result.returncode == 0:
        log.warning("No changes to commit — kiro-cli produced no diff.")
        return False

    commit_msg = f"fix: {summary} [{ticket_id}]\n\nsonar_hash: {sonar_hash}"
    _run(["git", "commit", "-m", commit_msg], cwd=repo_dir)
    return True


def step_push(repo_dir: Path, fix_branch: str) -> None:
    """Push the fix branch to origin."""
    _run(["git", "push", "origin", fix_branch], cwd=repo_dir)


def step_open_pr(repo_dir: Path, base_branch: str, fix_branch: str,
                 summary: str, ticket_id: str, ticket_url: str,
                 sonar_hash: str) -> str:
    """Open a GitHub PR and return the PR URL."""
    pr_title = f"fix: {summary} [{ticket_id}]"
    pr_body = (
        f"## Automated SonarQube Fix\n\n"
        f"**Ticket:** [{ticket_id}]({ticket_url})\n"
        f"**sonar_hash:** `{sonar_hash}`\n\n"
        f"This PR was generated automatically by kiro-minions.\n"
    )

    result = _run(
        [
            "gh", "pr", "create",
            "--base", base_branch,
            "--head", fix_branch,
            "--title", pr_title,
            "--body", pr_body,
        ],
        cwd=repo_dir,
    )
    pr_url = result.stdout.strip()
    log.info("PR opened: %s", pr_url)
    return pr_url


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(payload: dict) -> dict:
    """Execute the full fix pipeline. Returns a result dict."""
    repo_clone_url = payload["repo_clone_url"]
    base_branch = payload.get("base_branch", "main")
    fix_branch = payload.get("fix_branch") or f"kiro-minions/{payload['sonar_hash'][:12]}"
    file_to_fix = payload.get("file_to_fix")
    description = payload["description"]
    summary = payload.get("summary", "SonarQube fix")
    ticket_id = payload.get("ticket_id", "UNKNOWN")
    ticket_url = payload.get("ticket_url", "")
    sonar_hash = payload["sonar_hash"]

    # Defence in depth (issue #4): re-validate the ticket-derived values that
    # flow into git commands BEFORE running any shell command. A ValueError here
    # fails the pipeline safely before clone/branch/push happen.
    _validate_clone_url(repo_clone_url)
    _validate_branch_name(base_branch, field="base_branch")
    _validate_branch_name(fix_branch, field="fix_branch")

    with tempfile.TemporaryDirectory(prefix="kiro-minions-") as workdir:
        workdir = Path(workdir)

        # 0. Configure git auth via gh (token stays in the env, never in argv).
        log.info("=== STEP 0: Configure git auth ===")
        setup_git_auth()

        # 1. Clone
        log.info("=== STEP 1: Clone ===")
        repo_dir = step_clone(repo_clone_url, base_branch, workdir)

        # 2. Branch
        log.info("=== STEP 2: Create fix branch ===")
        step_create_branch(repo_dir, fix_branch)

        # 3-4. Fix + Test loop
        last_failure: str | None = None
        tests_passed = False

        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            log.info("=== STEP 3: kiro-cli fix (attempt %d/%d) ===", attempt, MAX_FIX_ATTEMPTS)
            step_kiro_fix(repo_dir, description, file_to_fix, attempt, last_failure)

            log.info("=== STEP 4: Run tests (attempt %d/%d) ===", attempt, MAX_FIX_ATTEMPTS)
            tests_passed, test_output = step_run_tests(repo_dir)

            if tests_passed:
                break

            last_failure = test_output
            log.warning("Attempt %d failed, will retry...", attempt)

            # Reset changes before retrying so kiro starts fresh
            if attempt < MAX_FIX_ATTEMPTS:
                _run(["git", "checkout", "--", "."], cwd=repo_dir)
                _run(["git", "clean", "-fd"], cwd=repo_dir)

        # 5. Commit
        log.info("=== STEP 5: Commit ===")
        has_changes = step_commit(repo_dir, summary, ticket_id, sonar_hash)
        if not has_changes:
            return {
                "status": "failed",
                "reason": "kiro-cli produced no changes",
                "attempts": MAX_FIX_ATTEMPTS,
            }

        # 6. Push
        log.info("=== STEP 6: Push ===")
        step_push(repo_dir, fix_branch)

        # 7. Open PR
        log.info("=== STEP 7: Open PR ===")
        pr_url = step_open_pr(
            repo_dir, base_branch, fix_branch,
            summary, ticket_id, ticket_url, sonar_hash,
        )

    return {
        "status": "success",
        "pr_url": pr_url,
        "tests_passed": tests_passed,
        "attempts": attempt,
    }


# ---------------------------------------------------------------------------
# AgentCore Runtime entrypoint
# ---------------------------------------------------------------------------


@app.entrypoint
async def entrypoint(payload: dict):
    """AgentCore Runtime invocation handler.

    Receives the TicketPayload from the orchestrator, runs the deterministic
    pipeline, and yields status messages as the stream response.
    """
    log.info("Received payload: ticket_id=%s sonar_hash=%s",
             payload.get("ticket_id"), payload.get("sonar_hash"))

    yield {"event": {"text": redact(
        f"Starting fix for {payload.get('ticket_id', 'unknown')}...\n"
    )}}

    try:
        result = run_pipeline(payload)
    except Exception as exc:
        log.exception("Pipeline failed")
        # str(exc) can echo command output/stderr; redact before it leaves here.
        result = {"status": "failed", "reason": redact(str(exc))}

    yield {"event": {"text": redact(json.dumps(result, indent=2))}}

    log.info("Pipeline result: %s", redact(json.dumps(result)))


# ---------------------------------------------------------------------------
# Module entry — run the AgentCore app server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run()
