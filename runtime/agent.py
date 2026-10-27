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

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_FIX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "3"))

# Timeout (seconds) for the per-ecosystem dependency install step (#9). Read at
# module level like MAX_FIX_ATTEMPTS so it can be tuned via the Runtime env.
INSTALL_TIMEOUT_S = int(os.environ.get("INSTALL_TIMEOUT_S", "600"))

# Timeout (seconds) for a single kiro-cli fix attempt (#10). Default 300 to match
# the previously hard-coded value; configurable via the Runtime env. A single
# attempt exceeding this raises subprocess.TimeoutExpired, which run_pipeline
# catches per attempt (recorded as that attempt's failure context) so one slow
# kiro run does not abort the whole job.
KIRO_TIMEOUT_S = int(os.environ.get("KIRO_TIMEOUT_S", "300"))

# Maximum size (chars) of truncated test output kept for the draft-PR body and
# (Group F) the retry brief. Head + tail are retained around this budget.
TEST_OUTPUT_BUDGET = 3000

# DynamoDB job table the runtime writes its terminal status to (#6). The
# orchestrator owns the same table; the runtime needs its own boto3 handle
# because it runs in a separate container. AWS_REGION is injected by the
# Runtime environment.
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "kiro-minions-jobs")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Repo allowlist re-check (#5), mirroring the orchestrator. Set out-of-band on
# the AgentCore Runtime env (like DYNAMODB_TABLE). Read at call time (not cached)
# so tests can monkeypatch the environment without a reload. Empty => allow-all.
REPO_ALLOWLIST_ENV = "REPO_ALLOWLIST"

# Terminal job statuses the runtime writes back.
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_TESTS_FAILED = "tests_failed"

# Coarse progress phases (ADR-003) the runtime emits as best-effort writes so
# the IDE status poll can show "queued -> cloning -> fixing -> testing ->
# opening_pr -> <terminal>". These string values MUST stay in sync with the
# orchestrator (orchestrator/handler.py: PHASE_* / PHASE_FOR_STATUS); a test
# asserts equality. The orchestrator sets PHASE_QUEUED on claim; the runtime
# writes the intermediate phases below without touching the terminal ``status``.
PHASE_QUEUED = "queued"
PHASE_CLONING = "cloning"
PHASE_FIXING = "fixing"
PHASE_TESTING = "testing"
PHASE_OPENING_PR = "opening_pr"
# Terminal phase mapping aligned to the terminal statuses the runtime writes.
PHASE_FOR_STATUS = {
    STATUS_DONE: "done",
    STATUS_FAILED: "failed",
    STATUS_TESTS_FAILED: "tests_failed",
}

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

# Module-level DynamoDB resource. Under the test stubs this resolves to the
# in-memory fake; in production it is the real boto3 resource. Tests monkeypatch
# ``_dynamodb`` (or point it at a FakeDynamoResource) to inspect writes.
_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


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


# Private copy of the owner/repo allowlist (#5). agent.py runs in a separate
# container and cannot import orchestrator/parser.py, so the allowlist logic is
# DUPLICATED here (defence in depth, like _CLONE_URL_RE / _BRANCH_RE).
_REPO_OWNER_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?$"
)


def _parse_allowlist(raw: str | None) -> list[str]:
    """Split a REPO_ALLOWLIST env string into owner/repo glob patterns."""
    if not raw:
        return []
    return [p for p in raw.replace(",", " ").split() if p]


def _repo_allowed(repo_clone_url: str, allowlist: list[str] | None) -> bool:
    """Return True if the repo's lowercased owner/repo matches the allowlist.

    Empty/None allowlist => allow-all (opt-in), same semantics as the
    orchestrator. A clone URL whose owner/repo cannot be extracted is rejected
    when an allowlist is configured (fail closed).
    """
    if not allowlist:
        return True
    if repo_clone_url is None:
        return False
    match = _REPO_OWNER_RE.match(repo_clone_url)
    if not match:
        return False
    owner_repo = f"{match.group(1)}/{match.group(2)}".lower()
    return any(
        fnmatch.fnmatchcase(owner_repo, pattern.strip().lower())
        for pattern in allowlist
        if pattern and pattern.strip()
    )


def _validate_repo_allowed(repo_clone_url: str) -> None:
    """Re-check the repo allowlist (#5); raise ValueError if disallowed.

    Reads ``REPO_ALLOWLIST`` from the environment at call time. When no allowlist
    is configured this is a no-op (allow-all). Raising a ValueError here fails
    the pipeline safely BEFORE any clone/subprocess runs; the entrypoint maps the
    exception to a failed terminal status.
    """
    allowlist = _parse_allowlist(os.environ.get(REPO_ALLOWLIST_ENV, ""))
    if not _repo_allowed(repo_clone_url, allowlist):
        raise ValueError(
            f"repo not in allowlist: {repo_clone_url!r}"
        )


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
# Terminal job status (issue #6: runtime writes its own lifecycle result)
# ---------------------------------------------------------------------------


def write_job_status(sonar_hash: str, status: str,
                     extra: dict | None = None) -> None:
    """Write the terminal job status to DynamoDB, keyed by sonar_hash.

    ``status`` is one of ``done`` / ``failed`` / ``tests_failed``. ``extra``
    carries additional attributes (e.g. ``pr_url`` on success or a REDACTED
    ``reason`` on failure). This is best-effort: a write failure is logged
    (redacted) and swallowed so it can never mask or replace the real pipeline
    result. Nothing written here embeds a secret — callers must redact any
    free-text ``reason`` before passing it in.
    """
    if not sonar_hash:
        log.warning("write_job_status: missing sonar_hash — skipping status write.")
        return
    try:
        table = _dynamodb.Table(DYNAMODB_TABLE)
        expr_names = {"#s": "status", "#u": "updated_at"}
        expr_values = {":s": status, ":u": int(time.time())}
        set_parts = ["#s = :s", "#u = :u"]
        for i, (k, v) in enumerate((extra or {}).items()):
            expr_names[f"#e{i}"] = k
            expr_values[f":e{i}"] = v
            set_parts.append(f"#e{i} = :e{i}")
        table.update_item(
            Key={"sonar_hash": sonar_hash},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except Exception as exc:  # noqa: BLE001 - never raise out of a status write
        log.warning("write_job_status failed: %s", redact(str(exc)))


def write_phase(sonar_hash: str, phase: str) -> None:
    """Update ONLY the coarse ``phase`` attribute on the job row (ADR-003).

    This is an ADDITIVE, best-effort progress write: it never changes the
    terminal ``status`` and never raises (matching the write_job_status
    contract), so a failed/slow phase write can never mask or replace the real
    pipeline result. ``updated_at`` is refreshed so the IDE sees the row move.
    """
    if not sonar_hash:
        log.warning("write_phase: missing sonar_hash — skipping phase write.")
        return
    try:
        table = _dynamodb.Table(DYNAMODB_TABLE)
        table.update_item(
            Key={"sonar_hash": sonar_hash},
            UpdateExpression="SET #p = :p, #u = :u",
            ExpressionAttributeNames={"#p": "phase", "#u": "updated_at"},
            ExpressionAttributeValues={":p": phase, ":u": int(time.time())},
        )
    except Exception as exc:  # noqa: BLE001 - never raise out of a status write
        log.warning("write_phase failed: %s", redact(str(exc)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_runner(cmd: str | list[str], cwd: str | Path | None = None,
                    check: bool = True,
                    timeout: int = 600,
                    input: str | None = None) -> subprocess.CompletedProcess:
    """Real subprocess-backed runner. Run a command, log it, and return the result.

    A list ``cmd`` runs with ``shell=False`` (the only safe form for any
    ticket-derived command, issue #4); a str ``cmd`` runs with ``shell=True``
    and is used ONLY for the auto-detected test command, which never contains
    ticket data. All displayed/logged text (the command line, STDOUT, STDERR)
    and the RuntimeError raised on non-zero exit are passed through redact() so
    secret values can never leak into logs or exception messages (issue #3).

    ``input`` (optional) is fed to the process via STDIN (never logged). This is
    how the kiro-cli brief is delivered (#11) so the possibly-large brief stays
    out of argv (E2BIG) and out of any logged command line.
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
        input=input,
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


# Single injectable indirection: tests can replace `RUNNER` (e.g. with a
# FakeRunner) so no real subprocess is spawned. All call sites go through
# `_run`, which delegates to whatever `RUNNER` currently points at.
#
# NOTE: there is deliberately NO _auth_clone_url helper. The token must never
# be injected into the clone URL (issue #3); git authenticates via the gh
# credential helper configured in setup_git_auth(), which reads GITHUB_TOKEN
# from the environment and keeps it out of argv and out of any logged URL.
RUNNER = _default_runner


def _run(cmd: str | list[str], cwd: str | Path | None = None, check: bool = True,
         timeout: int = 600, input: str | None = None) -> subprocess.CompletedProcess:
    """Run a command through the injectable RUNNER and return the result.

    ``input`` (optional) is forwarded to the RUNNER as the process STDIN. It
    defaults to None so every existing call site and every RUNNER/fake that does
    not expect stdin keeps working unchanged (#11).
    """
    return RUNNER(cmd, cwd=cwd, check=check, timeout=timeout, input=input)


def _first_failing_line(output: str) -> str | None:
    """Return the first line of ``output`` that looks like a test failure.

    Heuristic across the supported ecosystems (pytest / npm / mvn / make):
    lines containing FAILED/FAIL/ERROR markers, assertion errors, or a non-zero
    exit indicator. Returns None when nothing failure-shaped is found.
    """
    if not output:
        return None
    patterns = (
        "FAILED",
        "FAIL ",
        "AssertionError",
        "Error:",
        "ERROR",
        "BUILD FAILURE",
        "npm ERR!",
        "Traceback (most recent call last)",
        "✗",
    )
    for line in output.splitlines():
        for pat in patterns:
            if pat in line:
                return line.strip()
    return None


def _first_error_message_line(output: str) -> str | None:
    """Return the first failure-shaped line that is NOT a pytest ``FAILED <id>``.

    Used by ``_fingerprint_failure`` (review finding #2) to capture the
    actionable error MESSAGE (e.g. ``AssertionError: expected 2``) rather than
    the ``FAILED <test_id>`` summary line, which is already represented in the
    fingerprint's test-id set. Returns None when the only failure-shaped line is
    the ``FAILED`` summary (or nothing matched), in which case the fingerprint
    falls back to the test ids + first line alone.
    """
    if not output:
        return None
    patterns = (
        "AssertionError",
        "Error:",
        "ERROR",
        "BUILD FAILURE",
        "npm ERR!",
        "Traceback (most recent call last)",
        "✗",
    )
    for line in output.splitlines():
        stripped = line.strip()
        if _PYTEST_FAILED_RE.match(stripped):
            continue  # the FAILED <id> line is already in the id set
        for pat in patterns:
            if pat in stripped:
                return stripped
    return None


def truncate_test_output(output: str, budget: int = TEST_OUTPUT_BUDGET) -> str:
    """Truncate ``output`` to about ``budget`` chars, keeping head and tail.

    Always preserves the FIRST failing-test line (see ``_first_failing_line``)
    so the most actionable signal survives truncation. Used for the draft-PR
    body now and REUSED by Group F (#11) for the retry brief, so keep the
    signature stable: ``truncate_test_output(output: str, budget: int) -> str``.
    """
    if output is None:
        return ""
    if len(output) <= budget:
        return output

    marker = "\n...[truncated]...\n"
    first_fail = _first_failing_line(output)

    # Reserve room for the marker and (optionally) a surfaced first-failure line.
    fail_block = ""
    if first_fail and first_fail not in output[: budget // 2]:
        fail_block = f"\nFIRST FAILURE: {first_fail}\n"

    remaining = budget - len(marker) - len(fail_block)
    if remaining < 0:
        remaining = 0
    head_len = remaining // 2
    tail_len = remaining - head_len
    head = output[:head_len]
    tail = output[-tail_len:] if tail_len else ""
    return f"{head}{marker}{fail_block}{tail}"


# Volatile substrings that must be normalized out of a failure line before
# fingerprinting so the SAME logical failure hashes stable across attempts (#12):
# absolute paths, line/column numbers, hex/memory ids, timestamps, durations.
_HEX_ID_RE = re.compile(r"0x[0-9a-fA-F]+")
_ABS_PATH_RE = re.compile(r"(/[\w.\-]+)+")
_LINE_COL_RE = re.compile(r":\d+(?::\d+)?")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
)
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:s|ms|sec|secs|seconds)\b")
# Review finding #2: the previous `_NUMBER_RE = \b\d+\b` replaced EVERY bare
# integer with "<n>", so two attempts whose first error line differed only in a
# meaningful numeric value (e.g. "expected 1" vs "expected 2") fingerprinted
# identically and triggered a FALSE `repeated_failure` early-stop. Volatile
# numbers (line/col, durations, timestamps, hex/memory ids) are already stripped
# by the dedicated regexes above, so we only need to strip the remaining
# *contextually volatile* numbers rather than all of them. We target:
#   * memory-address-style decimal ids in "at 0x..."-less reprs, e.g.
#     "<object at 140234567890>" -> keep the shape, drop the address;
#   * explicit line/offset markers like "line 42", "col 7", "offset 1024";
#   * "0x"-less hex-looking tokens are left alone (too ambiguous).
# A genuinely different assertion value ("expected 2") now survives, so distinct
# numeric failures keep distinct fingerprints.
_LINE_WORD_RE = re.compile(r"\b(line|col|column|offset|address)\s+\d+\b", re.I)
_OBJ_ADDR_RE = re.compile(r"\b(object|instance)\s+at\s+\d+\b", re.I)
# pytest failing-test ids: "FAILED path/to/test.py::TestCls::test_name ...".
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(\S+)")


def _normalize_test_id(test_id: str) -> str:
    """Strip volatile path bits from a failing-test id, keeping the ``::test``.

    A pytest id like ``/home/alice/proj/tests/test_x.py::test_a`` and
    ``/var/ci/build/tests/test_x.py::test_a`` normalize to the same value so the
    same logical failure hashes stable regardless of the checkout location. The
    file's basename and the ``::`` node path are preserved (they ARE the signal).
    """
    if "::" in test_id:
        path_part, _, node_part = test_id.partition("::")
        basename = path_part.replace("\\", "/").rsplit("/", 1)[-1]
        return f"{basename}::{node_part}"
    return test_id.replace("\\", "/").rsplit("/", 1)[-1]


def _normalize_error_line(line: str) -> str:
    """Strip volatile bits from an error line so logically-equal lines match.

    Removes timestamps, absolute paths, :line:col suffixes, hex/memory ids,
    durations, and *contextually volatile* numbers (``line 42``, ``offset 7``,
    ``object at 140...``) then collapses whitespace. Used by
    ``_fingerprint_failure`` so retries that reproduce the same failure (but at
    a different path/line/time) hash to the same value, WHILE preserving
    meaningful numeric values (e.g. an assertion's ``expected 2``) so genuinely
    different numeric failures keep distinct fingerprints (review finding #2).
    """
    if not line:
        return ""
    out = _TIMESTAMP_RE.sub("<ts>", line)
    out = _HEX_ID_RE.sub("<hex>", out)
    out = _DURATION_RE.sub("<dur>", out)
    # Strip "line 42" / "col 7" / "offset 1024" / "object at 140..." style
    # volatile numbers BEFORE :line:col and path stripping so the keyword forms
    # collapse too, but leave standalone values (assertion expecteds) intact.
    out = _OBJ_ADDR_RE.sub(lambda m: m.group(0).rsplit(" ", 1)[0] + " <n>", out)
    out = _LINE_WORD_RE.sub(lambda m: m.group(1) + " <n>", out)
    out = _LINE_COL_RE.sub("", out)
    out = _ABS_PATH_RE.sub("<path>", out)
    return " ".join(out.split())


def _fingerprint_failure(test_output: str) -> str:
    """Return a stable fingerprint for a test-failure output (#12).

    Combines the SORTED set of failing-test identifiers (pytest ``FAILED ...``
    lines, with a generic fallback to the normalized first failing line) and the
    NORMALIZED first error line. The same logical failure therefore produces the
    same fingerprint across attempts even when volatile bits (paths, line
    numbers, timestamps, memory ids) differ, while genuinely different failures
    produce different fingerprints. Never leaks secrets: the inputs are already
    redacted test output and the result is a hash.
    """
    if not test_output:
        return ""

    failing_ids: set[str] = set()
    for line in test_output.splitlines():
        match = _PYTEST_FAILED_RE.match(line.strip())
        if match:
            # Normalize the id so the SAME test failing at a different absolute
            # path / line hashes stable (e.g. /home/.../tests/x.py::t and
            # /var/ci/.../tests/x.py::t collapse to the same key).
            failing_ids.add(_normalize_test_id(match.group(1)))

    normalized_first = _normalize_error_line(_first_failing_line(test_output) or "")

    if not failing_ids and normalized_first:
        # Generic fallback: no structured ids, key off the normalized line.
        failing_ids.add(normalized_first)

    # Review finding #2: the ``FAILED <id>`` line is ALSO captured in
    # ``failing_ids``, so when it is the first failing line the fingerprint
    # carries no detail beyond the test id. The actionable signal (e.g.
    # ``AssertionError: expected 2``) lives on a SEPARATE message line. Fold the
    # normalized error-MESSAGE line (the first failure-shaped line that is NOT a
    # pytest ``FAILED <id>`` line) into the basis so two attempts whose only
    # difference is a meaningful numeric value (``expected 1`` vs ``expected 2``)
    # fingerprint DIFFERENTLY, while volatile numbers remain normalized out.
    normalized_message = _normalize_error_line(
        _first_error_message_line(test_output) or ""
    )

    basis = (
        "|".join(sorted(failing_ids)) + "||" + normalized_first
        + "||" + normalized_message
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _detect_install_command(repo_dir: Path) -> list[str] | None:
    """Auto-detect the per-ecosystem dependency install command.

    Mirrors the signals used by ``_detect_test_command`` and ALWAYS returns an
    argv LIST (so it runs with shell=False; these commands carry no ticket data
    but we keep the list=shell=False invariant for everything but the detected
    test command). Returns None when no ecosystem is detected.

    * package.json + (package-lock.json OR npm-shrinkwrap.yaml) -> ['npm','ci']
      else ['npm','install']
    * requirements*.txt -> ['pip','install','-r', <file>] (prefer requirements.txt)
    * pyproject.toml / setup.py with a [test] extra -> ['pip','install','-e','.[test]']
    * pom.xml -> ['mvn','-q','dependency:resolve']
    """
    if (repo_dir / "package.json").exists():
        if (repo_dir / "package-lock.json").exists() or (
            repo_dir / "npm-shrinkwrap.yaml"
        ).exists():
            return ["npm", "ci"]
        return ["npm", "install"]

    # Python: prefer an explicit requirements file, then a [test] extra.
    req_file = _find_requirements_file(repo_dir)
    if req_file is not None:
        return ["pip", "install", "-r", req_file.name]

    if _has_test_extra(repo_dir):
        return ["pip", "install", "-e", ".[test]"]

    if (repo_dir / "pom.xml").exists():
        return ["mvn", "-q", "dependency:resolve"]

    return None


def _find_requirements_file(repo_dir: Path) -> Path | None:
    """Return requirements.txt if present, else the first requirements*.txt."""
    primary = repo_dir / "requirements.txt"
    if primary.exists():
        return primary
    matches = sorted(repo_dir.glob("requirements*.txt"))
    return matches[0] if matches else None


def _has_test_extra(repo_dir: Path) -> bool:
    """True if pyproject.toml or setup.py declares a ``test`` extra."""
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(errors="replace")
        # [project.optional-dependencies] test = [...] or extras_require style.
        if re.search(r"(?m)^\s*test\s*=", content):
            return True
        if "optional-dependencies" in content and "test" in content:
            return True
    setup_py = repo_dir / "setup.py"
    if setup_py.exists():
        content = setup_py.read_text(errors="replace")
        if "extras_require" in content and "test" in content:
            return True
    return False


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


# Common build / coverage / cache directories and file patterns that must NEVER
# be staged, even if kiro touched them (#13). Matched on path SEGMENTS (any
# component of a repo-relative path) plus a few whole-name / glob patterns so a
# churned artifact tree never pollutes the commit.
_STAGING_EXCLUDE_SEGMENTS = frozenset({
    "node_modules",
    ".pytest_cache",
    "__pycache__",
    "coverage",
    "htmlcov",
    "dist",
    "build",
    "target",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
})

# Whole-name files and suffix/glob patterns to exclude.
_STAGING_EXCLUDE_NAMES = frozenset({".coverage"})
_STAGING_EXCLUDE_GLOBS = ("*.egg-info", "*.pyc")


def _is_excluded_path(rel_path: str) -> bool:
    """True if a repo-relative path is a build/coverage/cache artifact (#13).

    Matches on any path SEGMENT against the excluded directory set, plus whole
    filename and suffix/glob patterns (``*.egg-info``, ``*.pyc``, ``.coverage``).
    """
    if not rel_path:
        return True
    normalized = rel_path.replace("\\", "/")
    segments = [seg for seg in normalized.split("/") if seg]
    for seg in segments:
        if seg in _STAGING_EXCLUDE_SEGMENTS:
            return True
        if seg in _STAGING_EXCLUDE_NAMES:
            return True
        for pattern in _STAGING_EXCLUDE_GLOBS:
            if fnmatch.fnmatchcase(seg, pattern):
                return True
    return False


def _git_status_porcelain(repo_dir: Path) -> set[str]:
    """Return the set of dirty/untracked repo-relative paths (#13).

    Runs ``git status --porcelain`` via _run (argv LIST, check=False) and parses
    each line into its path. ``--porcelain`` lines are ``XY <path>`` where ``XY``
    is a two-char status code; renames appear as ``orig -> new`` (we keep the new
    path). Quoted paths (``core.quotepath``) have their surrounding quotes
    stripped. Returns an EMPTY set on any non-zero status so a transient failure
    never mis-attributes artifacts to kiro.
    """
    result = _run(["git", "status", "--porcelain"], cwd=repo_dir, check=False)
    if result.returncode != 0:
        return set()
    paths: set[str] = set()
    for raw in (result.stdout or "").splitlines():
        if not raw.strip():
            continue
        # Porcelain v1: first two chars are the status code, then a space.
        entry = raw[3:] if len(raw) > 3 else raw.strip()
        # Renames/copies: "old -> new"; keep the destination path.
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip()
        if entry.startswith('"') and entry.endswith('"') and len(entry) >= 2:
            entry = entry[1:-1]
        if entry:
            paths.add(entry)
    return paths


def _paths_to_stage(post_install: set[str], post_fix: set[str]) -> list[str]:
    """Compute the sorted paths to stage: changed by kiro, not already churned.

    A path is staged when it is dirty/untracked AFTER the fix (``post_fix``) but
    was NOT already dirty in the POST-INSTALL snapshot (``post_install``) and is
    not an excluded build/coverage/cache artifact (#13).
    """
    candidates = post_fix - post_install
    return sorted(p for p in candidates if not _is_excluded_path(p))


def _build_kiro_brief(description: str, file_to_fix: str | None,
                      attempt: int, last_failure: str | None,
                      previous_diff: str | None = None) -> str:
    """Compose the kiro-cli brief text (delivered via STDIN, never argv).

    On the first attempt the brief is just the issue description (plus an
    optional file hint). On a retry it is prefixed with the TRUNCATED failing
    test output from the previous attempt and the TRUNCATED diff of what the
    previous attempt changed (#12), so a fresh kiro session sees the prior work
    and the failure it must address. All test output / diff is bounded via
    truncate_test_output; nothing unbounded is ever placed in the brief.
    """
    brief_parts: list[str] = []
    if attempt > 1 and last_failure:
        brief_parts.append(
            "PREVIOUS ATTEMPT FAILED. Test output:\n"
            f"{truncate_test_output(last_failure)}\n\n"
        )
        if previous_diff:
            brief_parts.append(
                "The previous attempt made the following changes (which did "
                "NOT fix the problem):\n"
                f"{truncate_test_output(previous_diff)}\n\n"
            )
        brief_parts.append(
            "Please fix the issue considering the test failure above.\n\n"
        )
    brief_parts.append(description)
    if file_to_fix:
        brief_parts.append(f"\n\nFocus on file: {file_to_fix}")
    return "".join(brief_parts)


def step_kiro_fix(repo_dir: Path, description: str, file_to_fix: str | None,
                  attempt: int, last_failure: str | None,
                  previous_diff: str | None = None) -> None:
    """Run kiro-cli headless to apply the fix.

    The brief is delivered via STDIN (``input=``), NOT as an argv element, so a
    large brief can never trigger E2BIG and never appears in a logged command
    line (#11). The kiro-cli command stays an argv LIST (shell=False). The
    per-attempt timeout is configurable via ``KIRO_TIMEOUT_S`` (#10); a timeout
    raises subprocess.TimeoutExpired, which run_pipeline catches per attempt.
    """
    brief = _build_kiro_brief(
        description, file_to_fix, attempt, last_failure, previous_diff,
    )
    # Defence in depth (#3): the brief is built from ticket text plus captured
    # test output/diff; run it through redact() so a secret value can never be
    # fed to kiro-cli via stdin either.
    _run(
        ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools"],
        cwd=repo_dir,
        timeout=KIRO_TIMEOUT_S,
        input=redact(brief),
    )


def step_install_deps(repo_dir: Path) -> None:
    """Install per-ecosystem dependencies BEFORE running tests (#9).

    Detects the install command (an argv LIST, shell=False) and runs it once
    with ``check=False`` and ``timeout=INSTALL_TIMEOUT_S``. A failing install is
    logged (redacted) but does NOT abort the pipeline: the failure surfaces via
    the baseline/test results, which is more informative than crashing here.
    """
    install_cmd = _detect_install_command(repo_dir)
    if install_cmd is None:
        log.info("No dependency install command detected; skipping install.")
        return

    log.info("Detected install command: %s", " ".join(install_cmd))
    result = _run(install_cmd, cwd=repo_dir, check=False, timeout=INSTALL_TIMEOUT_S)
    if result.returncode == 0:
        log.info("Dependency install succeeded.")
    else:
        log.warning(
            "Dependency install FAILED (rc=%d); continuing, failure will show "
            "in test results. stderr:\n%s",
            result.returncode,
            redact((result.stderr or "")[-1000:]),
        )


def _reset_worktree_for_retry(repo_dir: Path) -> None:
    """Reset the working tree between fix attempts, keeping deps deterministic.

    Review finding #1: the retry reset used ``git checkout -- .`` + ``git clean
    -fd`` while ``step_install_deps`` ran only ONCE before the loop. ``git clean
    -fd`` spares gitignored paths, so for ecosystems that gitignore their
    install output (``node_modules`` / ``.venv``) the installed tree survives.
    But any ecosystem/repo that does NOT gitignore its install output would run
    attempt 2+ against a partially-wiped dependency tree and fail for the WRONG
    reason (also feeding a misleading failure into the retry brief).

    The simple, safe fix is to RE-RUN the dependency install after the reset so
    retries are deterministic regardless of the repo's ignore rules. Install is
    idempotent (``npm ci`` / ``pip install -r`` / ``mvn dependency:resolve``) and
    already non-fatal (``check=False``), so re-running it is cheap when deps
    survived and correct when they did not. The RUNNER invariant holds: all three
    commands are argv LISTS (shell=False); see ``_detect_install_command``.
    """
    _run(["git", "checkout", "--", "."], cwd=repo_dir, check=False)
    _run(["git", "clean", "-fd"], cwd=repo_dir, check=False)
    # Re-install so a non-gitignored dependency tree wiped by `git clean -fd` is
    # restored before the next attempt's test run (#1).
    step_install_deps(repo_dir)


# Sentinel returned by step_run_tests when no test runner could be detected.
# "No tests detected" is NOT a pass (#8): the pipeline opens a draft PR for
# human review instead of a silent green PR.
NO_TESTS_DETECTED = "no tests detected"


def step_run_tests(repo_dir: Path) -> tuple[bool, str]:
    """Detect and run the test suite. Returns (passed, output).

    When no test runner is detected, returns ``(False, NO_TESTS_DETECTED)`` so
    "no tests detected" is a distinct, non-success outcome (#8). Callers must
    treat it specially (human-review draft PR) rather than as a pass.
    """
    test_cmd = _detect_test_command(repo_dir)
    if test_cmd is None:
        log.info("No test suite detected; this is NOT a pass (#8).")
        return False, NO_TESTS_DETECTED

    log.info("Detected test command: %s", test_cmd)
    result = _run(test_cmd, cwd=repo_dir, check=False, timeout=300)
    output = (result.stdout + "\n" + result.stderr).strip()
    passed = result.returncode == 0
    if passed:
        log.info("Tests PASSED.")
    else:
        log.warning("Tests FAILED (rc=%d).", result.returncode)
    return passed, output


def step_commit(repo_dir: Path, summary: str, ticket_id: str, sonar_hash: str,
                post_install: set[str] | None = None) -> tuple[bool, list[str]]:
    """Stage ONLY the paths kiro changed and commit (#13).

    Replaces the old ``git add -A`` (which swept build/test/cache artifacts into
    the commit) with SCOPED staging: it snapshots ``git status --porcelain``
    now (post-fix) and stages only the paths that became dirty/untracked SINCE
    the ``post_install`` snapshot, EXCLUDING common build/coverage/cache dirs and
    files (node_modules, .pytest_cache, __pycache__, dist, build, target, ...).

    Paths come from git output (not ticket data), but are still passed after a
    ``--`` separator in an argv LIST (shell=False) so a path can never be
    mis-parsed as a git option (defence in depth).

    Returns ``(had_changes, staged_files)``: ``had_changes`` is False (and
    ``staged_files`` empty) when nothing survives the exclusion filter OR the
    post-stage ``git diff --cached --quiet`` guard reports nothing staged.
    """
    post_install = post_install or set()
    post_fix = _git_status_porcelain(repo_dir)
    to_stage = _paths_to_stage(post_install, post_fix)

    if not to_stage:
        log.warning(
            "No kiro-authored changes to commit (after excluding build/cache "
            "artifacts) — kiro-cli produced no diff."
        )
        return False, []

    # Stage only the surviving paths. `--` separates options from pathspecs so
    # a path can never be parsed as a git option.
    _run(["git", "add", "--", *to_stage], cwd=repo_dir)

    # Guard: confirm something is actually staged (handles the edge case where
    # every candidate was e.g. already-ignored by .gitignore).
    result = _run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir, check=False)
    if result.returncode == 0:
        log.warning("No changes staged — kiro-cli produced no committable diff.")
        return False, []

    commit_msg = f"fix: {summary} [{ticket_id}]\n\nsonar_hash: {sonar_hash}"
    _run(["git", "commit", "-m", commit_msg], cwd=repo_dir)
    log.info("Committed %d staged path(s).", len(to_stage))
    return True, to_stage


def step_push(repo_dir: Path, fix_branch: str) -> None:
    """Push the fix branch to origin."""
    _run(["git", "push", "origin", fix_branch], cwd=repo_dir)


def _build_pr_body(ticket_id: str, ticket_url: str, sonar_hash: str,
                   *, draft: bool, no_tests: bool,
                   tests_passed: bool,
                   baseline_passed: bool | None,
                   test_output: str | None,
                   staged_files: list[str] | None,
                   repeated_failure: bool = False) -> str:
    """Compose the PR body (always routed through redact() by the caller).

    Draft PRs surface WHY they need review: the truncated failing output and the
    baseline (pre-fix) result, or a human-review note when no tests were found.
    """
    lines = [
        "## Automated SonarQube Fix",
        "",
        f"**Ticket:** [{ticket_id}]({ticket_url})",
        f"**sonar_hash:** `{sonar_hash}`",
        "",
    ]

    if no_tests:
        lines += [
            "> :warning: **NO TESTS DETECTED - HUMAN REVIEW REQUIRED**",
            "",
            "No test runner could be auto-detected for this repository, so the "
            "fix is **unverified**. This PR is opened as a draft for a human to "
            "review and validate before merging.",
            "",
        ]
    elif draft and not tests_passed:
        baseline_str = (
            "green (passed before the fix)" if baseline_passed
            else "red (already failing before the fix)"
            if baseline_passed is not None else "unknown"
        )
        lines += [
            "> :warning: **TESTS STILL FAILING - HUMAN REVIEW REQUIRED**",
            "",
            f"**Baseline (pre-fix) result:** {baseline_str}",
            "",
        ]
        if repeated_failure:
            lines += [
                "**Stopped early:** two consecutive fix attempts produced the "
                "same failure fingerprint (`repeated_failure`), so the retry "
                "loop stopped before exhausting all attempts - further "
                "automated attempts were unlikely to help.",
                "",
            ]
        lines += [
            "The automated fix did not make the test suite pass. This PR is "
            "opened as a draft with the failing output below for review.",
            "",
            "<details><summary>Truncated failing test output</summary>",
            "",
            "```",
            (test_output or "").strip(),
            "```",
            "",
            "</details>",
            "",
        ]

    # Staged-file list placeholder (Group G / #13 populates this list).
    if staged_files:
        lines += ["### Staged files", ""]
        lines += [f"- `{path}`" for path in staged_files]
        lines += [""]

    lines += ["This PR was generated automatically by kiro-minions.", ""]
    return "\n".join(lines)


def step_open_pr(repo_dir: Path, base_branch: str, fix_branch: str,
                 summary: str, ticket_id: str, ticket_url: str,
                 sonar_hash: str, *, draft: bool = False,
                 no_tests: bool = False, tests_passed: bool = True,
                 baseline_passed: bool | None = None,
                 test_output: str | None = None,
                 staged_files: list[str] | None = None,
                 repeated_failure: bool = False) -> str:
    """Open a GitHub PR and return the PR URL.

    ``draft=True`` appends ``--draft`` to the ``gh pr create`` argv (keeping the
    gh-pr-create-via-RUNNER argv-list shape; tests use FakeRunner, never real
    gh). The body is built by ``_build_pr_body`` and ALWAYS passed through
    redact() so no secret can leak into the PR body. No secret is ever placed in
    the gh argv.
    """
    pr_title = f"fix: {summary} [{ticket_id}]"
    if draft:
        pr_title = f"[DRAFT] {pr_title}"

    pr_body = redact(_build_pr_body(
        ticket_id, ticket_url, sonar_hash,
        draft=draft, no_tests=no_tests, tests_passed=tests_passed,
        baseline_passed=baseline_passed, test_output=test_output,
        staged_files=staged_files, repeated_failure=repeated_failure,
    ))

    argv = [
        "gh", "pr", "create",
        "--base", base_branch,
        "--head", fix_branch,
        "--title", pr_title,
        "--body", pr_body,
    ]
    if draft:
        argv.append("--draft")
    if no_tests:
        # Label the PR so a human knows it needs review (gh dedupes/creates the
        # label as needed; a missing label would only warn, not fail the fix).
        argv += ["--label", "needs-human-review"]

    result = _run(argv, cwd=repo_dir)
    pr_url = result.stdout.strip()
    log.info("PR opened (draft=%s): %s", draft, pr_url)
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

    # Repo allowlist re-check (#5): defence in depth. Raises before step_clone
    # (and any subprocess) when a REPO_ALLOWLIST is configured and the repo is
    # not allowed; a no-op when unconfigured.
    _validate_repo_allowed(repo_clone_url)

    with tempfile.TemporaryDirectory(prefix="kiro-minions-") as workdir:
        workdir = Path(workdir)

        # 0. Configure git auth via gh (token stays in the env, never in argv).
        log.info("=== STEP 0: Configure git auth ===")
        setup_git_auth()

        # 1. Clone
        log.info("=== STEP 1: Clone ===")
        # ADR-003 coarse progress: best-effort phase write (never raises, never
        # changes the terminal status) so the IDE poll shows "cloning".
        write_phase(sonar_hash, PHASE_CLONING)
        repo_dir = step_clone(repo_clone_url, base_branch, workdir)

        # 2. Branch
        log.info("=== STEP 2: Create fix branch ===")
        step_create_branch(repo_dir, fix_branch)

        # Post-clone snapshot (#13): the working-tree state right after clone +
        # branch, BEFORE dependency install churns anything. Kept for context /
        # debugging; the staging baseline is the post-install snapshot below.
        post_clone_snapshot = _git_status_porcelain(repo_dir)
        log.info("Post-clone dirty paths: %d", len(post_clone_snapshot))

        # 2b. Install dependencies ONCE after clone+branch (#9), before the
        # baseline run and before the fix loop. Not re-run per attempt.
        log.info("=== STEP 2b: Install dependencies ===")
        step_install_deps(repo_dir)

        # Post-install snapshot (#13): everything dependency install already
        # churned (lockfiles, caches, vendored dirs). This is the baseline for
        # scoped staging so install artifacts are NOT attributed to kiro.
        post_install_snapshot = _git_status_porcelain(repo_dir)
        log.info("Post-install dirty paths: %d", len(post_install_snapshot))

        # Whether a test runner exists at all (#8): no runner => human review.
        # Review finding #4 (INTENTIONAL): no_tests is computed ONCE here, before
        # the fix loop, so a repo with no detectable runner stays on the
        # human-review draft path even if kiro were to author a test suite during
        # the fix. This is deliberate for THIS task: auto-generating a test
        # harness is out of scope, and routing an unverified fix to a human is
        # the safe default. If test generation becomes in scope, re-detect the
        # runner after the fix loop and re-evaluate this flag.
        no_tests = _detect_test_command(repo_dir) is None

        # 2c. Baseline test run ONCE before the first kiro fix (#9). Records
        # whether the suite was green pre-fix, distinguishing pre-existing
        # failures from regressions. Skipped (and left unknown) when no runner.
        baseline_passed: bool | None = None
        baseline_output = ""
        if no_tests:
            log.info("=== STEP 2c: Baseline SKIPPED (no tests detected) ===")
        else:
            log.info("=== STEP 2c: Baseline test run (pre-fix) ===")
            baseline_passed, baseline_output = step_run_tests(repo_dir)
            log.info("Baseline result: %s",
                     "GREEN" if baseline_passed else "RED")

        # 3-4. Fix + Test loop
        # ADR-003: entering the kiro fix loop -> "fixing".
        write_phase(sonar_hash, PHASE_FIXING)
        last_failure: str | None = None
        tests_passed = False
        test_output = ""
        attempt = 0
        # Diff captured from the previous attempt (#12): fed into the next
        # brief so a fresh kiro session sees what the prior attempt changed.
        previous_diff: str | None = None
        # Previous attempt's failure fingerprint (#12): two consecutive attempts
        # sharing a fingerprint means more kiro runs won't help -> stop early.
        previous_fingerprint: str | None = None
        repeated_failure = False

        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            log.info("=== STEP 3: kiro-cli fix (attempt %d/%d) ===", attempt, MAX_FIX_ATTEMPTS)
            try:
                step_kiro_fix(repo_dir, description, file_to_fix, attempt,
                              last_failure, previous_diff)
            except subprocess.TimeoutExpired:
                # #10: one slow kiro attempt must not abort the whole job. Record
                # a redacted timeout as this attempt's failure context and retry.
                timeout_msg = redact(
                    f"kiro-cli timed out after {KIRO_TIMEOUT_S} seconds"
                )
                log.warning("Attempt %d: %s", attempt, timeout_msg)
                last_failure = timeout_msg
                tests_passed = False
                # Reset the tree (and re-install deps, #1) so the next attempt
                # starts from a clean, fully-provisioned base.
                if attempt < MAX_FIX_ATTEMPTS:
                    _reset_worktree_for_retry(repo_dir)
                previous_diff = None
                previous_fingerprint = None
                continue

            # When no test runner exists we still apply the fix once but cannot
            # verify it; break out to the human-review draft-PR path.
            if no_tests:
                break

            log.info("=== STEP 4: Run tests (attempt %d/%d) ===", attempt, MAX_FIX_ATTEMPTS)
            # ADR-003: running the test suite -> "testing".
            write_phase(sonar_hash, PHASE_TESTING)
            tests_passed, test_output = step_run_tests(repo_dir)

            if tests_passed:
                break

            last_failure = test_output
            log.warning("Attempt %d failed, will retry...", attempt)

            # #12: fingerprint this failure. If it matches the previous attempt's
            # fingerprint the fix is stuck in a loop; stop early and open a draft
            # PR noting repeated_failure.
            fingerprint = _fingerprint_failure(test_output)
            if previous_fingerprint is not None and fingerprint == previous_fingerprint:
                log.warning(
                    "Attempt %d produced the SAME failure fingerprint as the "
                    "previous attempt; stopping early (repeated_failure).",
                    attempt,
                )
                repeated_failure = True
                break
            previous_fingerprint = fingerprint

            # Reset changes before retrying so kiro starts fresh. Capture the
            # diff of what THIS attempt changed BEFORE the reset (#12) so it can
            # be fed into the next attempt's brief.
            if attempt < MAX_FIX_ATTEMPTS:
                diff_result = _run(["git", "diff"], cwd=repo_dir, check=False)
                previous_diff = (diff_result.stdout or "").strip() or None
                # Reset AND re-install deps (#1) so the retry's test run is
                # deterministic even for ecosystems that do not gitignore their
                # install output.
                _reset_worktree_for_retry(repo_dir)

        # 5. Commit — scoped staging (#13): only the paths kiro changed since the
        # post-install snapshot, excluding build/coverage/cache artifacts.
        log.info("=== STEP 5: Commit ===")
        has_changes, staged_files = step_commit(
            repo_dir, summary, ticket_id, sonar_hash,
            post_install=post_install_snapshot,
        )
        if not has_changes:
            return {
                "status": "failed",
                "reason": "kiro-cli produced no changes",
                "attempts": attempt,
                "draft": False,
                "tests_passed": tests_passed,
                "baseline": {"passed": baseline_passed,
                             "output": truncate_test_output(baseline_output)},
                "staged_files": [],
            }

        # 6. Push
        log.info("=== STEP 6: Push ===")
        step_push(repo_dir, fix_branch)

        # Decide the PR mode (#8):
        #   no tests detected       -> DRAFT PR, human review, tests_failed
        #   tests failing after all -> DRAFT PR (failing output + baseline),
        #                              status tests_failed
        #   tests passing           -> NORMAL PR, status success
        truncated_output = truncate_test_output(test_output)
        baseline_result = {
            "passed": baseline_passed,
            "output": truncate_test_output(baseline_output),
        }

        # 7. Open PR
        log.info("=== STEP 7: Open PR (no_tests=%s tests_passed=%s) ===",
                 no_tests, tests_passed)
        # ADR-003: about to open the PR -> "opening_pr". Best-effort; the
        # terminal status/phase is written by the entrypoint once the pipeline
        # result is known.
        write_phase(sonar_hash, PHASE_OPENING_PR)
        if no_tests:
            pr_url = step_open_pr(
                repo_dir, base_branch, fix_branch, summary, ticket_id,
                ticket_url, sonar_hash, draft=True, no_tests=True,
                tests_passed=False, baseline_passed=baseline_passed,
                staged_files=staged_files,
            )
            return {
                "status": STATUS_TESTS_FAILED,
                "reason": "no tests detected: unverified fix, human review needed",
                "pr_url": pr_url,
                "draft": True,
                "no_tests": True,
                "tests_passed": False,
                "baseline": baseline_result,
                "test_output": NO_TESTS_DETECTED,
                "attempts": attempt,
                "staged_files": staged_files,
            }

        if not tests_passed:
            reason = (
                "repeated_failure: two consecutive attempts produced the same "
                "failure fingerprint, stopped early"
                if repeated_failure
                else "tests still failing after all fix attempts"
            )
            pr_url = step_open_pr(
                repo_dir, base_branch, fix_branch, summary, ticket_id,
                ticket_url, sonar_hash, draft=True, no_tests=False,
                tests_passed=False, baseline_passed=baseline_passed,
                test_output=truncated_output, staged_files=staged_files,
                repeated_failure=repeated_failure,
            )
            return {
                "status": STATUS_TESTS_FAILED,
                "reason": reason,
                "repeated_failure": repeated_failure,
                "pr_url": pr_url,
                "draft": True,
                "no_tests": False,
                "tests_passed": False,
                "baseline": baseline_result,
                "test_output": truncated_output,
                "attempts": attempt,
                "staged_files": staged_files,
            }

        pr_url = step_open_pr(
            repo_dir, base_branch, fix_branch, summary, ticket_id,
            ticket_url, sonar_hash, draft=False, no_tests=False,
            tests_passed=True, baseline_passed=baseline_passed,
            staged_files=staged_files,
        )

    return {
        "status": "success",
        "pr_url": pr_url,
        "draft": False,
        "no_tests": False,
        "tests_passed": tests_passed,
        "baseline": baseline_result,
        "test_output": truncated_output,
        "attempts": attempt,
        "staged_files": staged_files,
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

    # Routing (ADR-002 Option C): a 'spec' task offloads a WHOLE tasks.md to the
    # spec-executor worker. Branch EARLY and delegate so the one_shot path below
    # stays behaviourally unchanged. The default/absent task_type is one_shot.
    # spec_agent is imported lazily to avoid an import cycle (it imports agent).
    if payload.get("task_type") == "spec":
        import spec_agent

        async for item in spec_agent.run_spec_dispatch(payload):
            yield item
        return

    sonar_hash = payload.get("sonar_hash")

    yield {"event": {"text": redact(
        f"Starting fix for {payload.get('ticket_id', 'unknown')}...\n"
    )}}

    try:
        # #14: run the blocking pipeline off the event loop so /ping health
        # checks stay responsive for the duration of a long fix.
        result = await asyncio.to_thread(run_pipeline, payload)
    except Exception as exc:
        log.exception("Pipeline failed")
        # str(exc) can echo command output/stderr; redact before it leaves here.
        reason = redact(str(exc))
        result = {"status": "failed", "reason": reason}
        # #6: an unexpected exception is a hard failure. ADR-003: also record a
        # terminal phase alongside the status (additive; status is unchanged).
        write_job_status(
            sonar_hash, STATUS_FAILED,
            {"reason": reason, "phase": PHASE_FOR_STATUS[STATUS_FAILED]},
        )
    else:
        # #6/#8: translate the pipeline result into a terminal DynamoDB status.
        #   success                         -> done + pr_url
        #   tests_failed (still-failing)    -> tests_failed + redacted reason
        #   tests_failed (no tests detected)-> tests_failed + redacted reason
        #   failed (kiro produced no diff)  -> tests_failed + redacted reason
        # handler treats tests_failed as RETRYABLE, which is the desired
        # semantics: a human/adjusted ticket can re-dispatch the job.
        if result.get("status") == "success":
            write_job_status(
                sonar_hash, STATUS_DONE,
                {"pr_url": result.get("pr_url", ""),
                 "phase": PHASE_FOR_STATUS[STATUS_DONE]},
            )
        else:
            write_job_status(
                sonar_hash, STATUS_TESTS_FAILED,
                {"reason": redact(str(result.get("reason", ""))),
                 "phase": PHASE_FOR_STATUS[STATUS_TESTS_FAILED]},
            )

    yield {"event": {"text": redact(json.dumps(result, indent=2))}}

    log.info("Pipeline result: %s", redact(json.dumps(result)))


# ---------------------------------------------------------------------------
# Module entry — run the AgentCore app server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run()
