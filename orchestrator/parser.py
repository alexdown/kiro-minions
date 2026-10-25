"""Pure-function parsers for the Jira webhook body.

No AWS dependencies. These functions extract the machine-readable fields from
the ticket description's Part 1 structured header block, and the sonar_hash
from the labels array.
"""

from __future__ import annotations

import re

# Mapping of (lowercased) line-prefix key -> payload field name.
# Parsing is line-prefix based and case-insensitive on the keys.
_HEADER_KEYS = {
    "clone": "repo_clone_url",
    "base branch": "base_branch",
    "file to fix": "file_to_fix",
    "suggested fix branch": "fix_branch",
}

_SONAR_HASH_PREFIX = "sonar-hash:"

# ---------------------------------------------------------------------------
# Validation (issue #4: shell-injection defence)
#
# Ticket-derived values (branch names, clone URL) flow directly into git/gh
# commands in the runtime. They MUST be validated strictly before use. These
# patterns are intentionally conservative; the same logic is DUPLICATED in
# runtime/agent.py (which runs in a separate container and cannot import this
# module) for defence in depth.
# ---------------------------------------------------------------------------

# A branch name: 1-200 chars from a tight allow-list. Additional rules (no
# leading '-', no '..' substring) are enforced separately below.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

# A clone URL: exactly https://github.com/<owner>/<repo> with an optional single
# trailing ".git". No userinfo/@, no port, no query, no fragment, no extra path
# segments. <owner> and <repo> use a conservative GitHub-name allow-list.
_CLONE_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?:\.git)?$"
)


def validate_branch_name(name: str, field: str = "branch") -> str:
    """Validate a git branch name against the strict allow-list.

    Enforces ``^[A-Za-z0-9._/-]{1,200}$``, rejects a leading ``-`` (which git
    would treat as an option) and rejects any ``..`` substring. Returns the name
    unchanged on success; raises ``ValueError`` (naming the field and echoing the
    rejected ticket value, which is not a secret) on any violation.
    """
    if name is None or not _BRANCH_RE.match(name):
        raise ValueError(
            f"invalid {field}: {name!r} does not match ^[A-Za-z0-9._/-]{{1,200}}$"
        )
    if name.startswith("-"):
        raise ValueError(f"invalid {field}: {name!r} must not start with '-'")
    if ".." in name:
        raise ValueError(f"invalid {field}: {name!r} must not contain '..'")
    return name


def validate_clone_url(url: str, field: str = "repo_clone_url") -> str:
    """Validate a GitHub HTTPS clone URL.

    Accepts ONLY ``https://github.com/<owner>/<repo>`` or the same with a single
    trailing ``.git``. Rejects any other scheme, host, userinfo, port, query,
    fragment, or extra path segments. Returns the URL unchanged on success;
    raises ``ValueError`` on any violation.
    """
    if url is None or not _CLONE_URL_RE.match(url):
        raise ValueError(
            f"invalid {field}: {url!r} is not a https://github.com/<owner>/<repo>"
            " clone URL"
        )
    return url


def parse_description_header(description: str) -> dict:
    """Parse the structured Part 1 header block from a Jira description.

    The header is line-prefix based, e.g.::

        Repo URL: https://github.com/your-org/your-repo
        Clone: https://github.com/your-org/your-repo.git
        Base branch: master
        File to fix: server.js:135
        Suggested fix branch: sonarqube-fix/xss-swig-autoescape

    Returns a dict with keys: ``repo_clone_url``, ``base_branch``,
    ``fix_branch``, ``file_to_fix``.

    - ``repo_clone_url`` is **required**: raises ``ValueError`` if the
      ``Clone:`` line is absent or empty.
    - ``base_branch`` defaults to ``"main"`` when absent.
    - ``fix_branch`` and ``file_to_fix`` default to ``None`` when absent.

    The match is case-insensitive on the key portion. Unrecognized lines are
    ignored. The header block is *not* stripped from the description elsewhere;
    this function only reads it.
    """
    if description is None:
        raise ValueError("description is None; cannot parse header block")

    parsed: dict = {}

    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key_part, _, value_part = line.partition(":")
        key = key_part.strip().lower()
        field = _HEADER_KEYS.get(key)
        if field is None:
            continue
        value = value_part.strip()
        if not value:
            continue
        # First occurrence wins; don't let later lines clobber.
        parsed.setdefault(field, value)

    repo_clone_url = parsed.get("repo_clone_url")
    if not repo_clone_url:
        raise ValueError(
            "description header is missing a required 'Clone:' line "
            "(repo_clone_url); rejecting ticket"
        )

    # Strict validation (issue #4). repo_clone_url is required; base_branch
    # defaults to 'main' and is always validated; fix_branch is optional but is
    # validated when present. file_to_fix is not interpolated into a shell
    # command (it is only embedded in the kiro-cli brief argv), so it keeps its
    # existing pass-through behavior.
    validate_clone_url(repo_clone_url)

    base_branch = parsed.get("base_branch", "main")
    validate_branch_name(base_branch, field="base_branch")

    fix_branch = parsed.get("fix_branch")
    if fix_branch is not None:
        validate_branch_name(fix_branch, field="fix_branch")

    return {
        "repo_clone_url": repo_clone_url,
        "base_branch": base_branch,
        "fix_branch": fix_branch,
        "file_to_fix": parsed.get("file_to_fix"),
    }


def extract_sonar_hash(labels: list[str]) -> str | None:
    """Find the ``sonar-hash:<key>`` label and return ``<key>``.

    The match on the prefix is case-insensitive; the returned value preserves
    the original case of the hash. Returns ``None`` if no such label exists.
    """
    if not labels:
        return None
    for label in labels:
        if label is None:
            continue
        stripped = label.strip()
        if stripped.lower().startswith(_SONAR_HASH_PREFIX):
            value = stripped[len(_SONAR_HASH_PREFIX):].strip()
            return value or None
    return None


def has_sonarqube_label(labels: list[str]) -> bool:
    """Return True if the labels indicate a SonarQube remediation ticket.

    Accepts either ``sonarqube`` or ``SONARQUBE-FIX`` (case-insensitive).
    """
    if not labels:
        return False
    wanted = {"sonarqube", "sonarqube-fix"}
    return any(
        (label or "").strip().lower() in wanted for label in labels
    )
