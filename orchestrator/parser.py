"""Pure-function parsers for the Jira webhook body.

No AWS dependencies. These functions extract the machine-readable fields from
the ticket description's Part 1 structured header block, and the sonar_hash
from the labels array.
"""

from __future__ import annotations

# Mapping of (lowercased) line-prefix key -> payload field name.
# Parsing is line-prefix based and case-insensitive on the keys.
_HEADER_KEYS = {
    "clone": "repo_clone_url",
    "base branch": "base_branch",
    "file to fix": "file_to_fix",
    "suggested fix branch": "fix_branch",
}

_SONAR_HASH_PREFIX = "sonar-hash:"


def parse_description_header(description: str) -> dict:
    """Parse the structured Part 1 header block from a Jira description.

    The header is line-prefix based, e.g.::

        Repo URL: https://github.com/alexdown/NodeGoat
        Clone: https://github.com/alexdown/NodeGoat.git
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

    return {
        "repo_clone_url": repo_clone_url,
        "base_branch": parsed.get("base_branch", "main"),
        "fix_branch": parsed.get("fix_branch"),
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
