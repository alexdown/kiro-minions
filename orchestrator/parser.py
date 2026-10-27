"""Pure-function parsers for the Jira webhook body.

No AWS dependencies. These functions extract the machine-readable fields from
the ticket description's Part 1 structured header block, and the sonar_hash
from the labels array.

This module also hosts the pure mapper for the Kiro IDE ingress (FEAT-002):
``parse_ide_request`` validates and maps an IDE task request JSON into the
``TaskPayload`` field subset, reusing ``validate_clone_url`` /
``validate_branch_name`` unchanged. See ADR-002 (scoped task shape), ADR-005
(payload superset).
"""

from __future__ import annotations

import fnmatch
import hashlib
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


# A repo reference extracted from a validated clone URL: owner/repo (no .git).
# Reuses the same host/shape as _CLONE_URL_RE but captures owner + repo.
_REPO_OWNER_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?$"
)


def _owner_repo(clone_url: str) -> str | None:
    """Extract the lowercased ``owner/repo`` from a validated clone URL.

    Returns ``None`` when the URL does not match the expected github.com shape
    (callers should have validated it with ``validate_clone_url`` first).
    """
    if clone_url is None:
        return None
    match = _REPO_OWNER_RE.match(clone_url)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    return f"{owner}/{repo}".lower()


def repo_allowed(repo_clone_url: str, allowlist: list[str] | None) -> bool:
    """Return True if the repo's ``owner/repo`` matches the allowlist.

    ``allowlist`` is a list of ``owner/repo`` glob patterns (e.g. ``your-org/*``
    or ``your-org/your-repo``), matched case-insensitively with
    ``fnmatch.fnmatchcase`` against the lowercased ``owner/repo`` extracted from
    the clone URL. An empty/``None`` allowlist means "no allowlist configured"
    and allows ALL repos (the allowlist is opt-in). This is a SEPARATE check
    from ``validate_clone_url`` (which only enforces the github.com URL shape).

    A clone URL whose owner/repo cannot be extracted is rejected when an
    allowlist is configured (fail closed), and allowed when it is not.
    """
    if not allowlist:
        return True
    owner_repo = _owner_repo(repo_clone_url)
    if owner_repo is None:
        return False
    return any(
        fnmatch.fnmatchcase(owner_repo, pattern.strip().lower())
        for pattern in allowlist
        if pattern and pattern.strip()
    )


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


# ---------------------------------------------------------------------------
# Kiro IDE ingress (FEAT-002): request validation + mapping to TaskPayload
#
# An IDE "task" carries a ``task_type`` discriminator (ADR-002 Option C):
#   * ``one_shot`` — ONE scoped instruction (-> description -> kiro-cli brief),
#     an optional target file/area hint (-> file_to_fix); the original scoped
#     behaviour, mapped ~1:1 onto the deterministic pipeline.
#   * ``spec`` — a WHOLE tasks.md offloaded to the spec-executor worker, which
#     reads the whole task list and iterates task-by-task (one commit per task,
#     one PR per spec). The task list is delivered inline (``tasks_md``) or by a
#     traversal-safe repo-relative reference (``tasks_md_path``).
# Acceptance = tests_pass (v1 fixed). The mapper below is pure (no AWS); the
# handler layer owns auth, idempotency, the allowlist gate, and the enqueue. It
# REUSES the URL/branch validators above unchanged (ADR-005 keeps the
# runtime-read field names).
# ---------------------------------------------------------------------------

# v1 acceptance is fixed to "tests pass" (ADR-002). Future: lint, typecheck, …
IDE_ACCEPTANCE = "tests_pass"

# Task-type discriminator (ADR-002 Option C / ADR-005). A ``one_shot`` task is a
# single scoped instruction mapped ~1:1 onto the existing deterministic pipeline
# (the original behaviour, kept exactly). A ``spec`` task offloads a WHOLE
# ``tasks.md`` to the spec-executor worker, which reads the task list and
# iterates task-by-task. ``one_shot`` is the default when the field is absent so
# every existing IDE request keeps its current behaviour.
TASK_TYPE_ONE_SHOT = "one_shot"
TASK_TYPE_SPEC = "spec"
TASK_TYPES = frozenset({TASK_TYPE_ONE_SHOT, TASK_TYPE_SPEC})

# A repo-relative path to a tasks.md inside the clone (e.g.
# ``.kiro/specs/<name>/tasks.md``). This is a SECURITY control: the value may be
# interpolated into a filesystem read inside the clone, so it must never escape
# the clone root nor reach a shell as an option. The charset is tight (no
# backslash, no NUL, no whitespace), and ``..`` segments / leading ``/`` are
# rejected separately below. Length is capped to keep it bounded.
_REPO_RELATIVE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")

# Every IDE task_id — derived OR client-supplied — carries this prefix so it
# lives in its own namespace on the table shared with the Jira path (where the
# physical ``sonar_hash`` attribute holds a SonarQube hash) and the per-actor
# quota counters (``quota#<actor>#<window>``). A client ``idempotency_key`` is
# constrained to this namespace (see ``_normalise_idempotency_key``) so a caller
# can never supply a value that collides with a Jira hash or a ``quota#`` counter
# item, and can never read/wedge another tenant's row via the partition key.
IDE_TASK_ID_PREFIX = "ide-"

# Reserved physical-key prefixes a client override must never be allowed to
# reach, even after the ``ide-`` prefix is enforced. ``quota#`` is the per-actor
# counter namespace; listing it here documents the collision the normalisation
# prevents and fails closed if the prefix rule is ever loosened.
_RESERVED_KEY_PREFIXES = ("quota#",)

# A client-supplied idempotency key (the part AFTER the enforced ``ide-``
# prefix) must be a bounded, conservative token so the resulting partition key
# stays within a safe charset and length. No ``#`` (keeps it out of the
# ``quota#`` namespace), no path/space/control characters.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class IdeRequestError(ValueError):
    """Raised when an IDE task request is missing/invalid (maps to HTTP 400)."""


def validate_repo_relative_path(path: str, field: str = "tasks_md_path") -> str:
    """Validate a conservative, traversal-safe repo-relative POSIX path.

    This is a SECURITY control for the spec-executor's ``tasks_md_path``: the
    value points at a file INSIDE the clone and may be joined onto the clone
    root, so it must never escape the clone or reach a shell as an option.

    Rules (raises :class:`IdeRequestError` on any violation):
      * must be a non-empty string, ``<= 256`` chars, matching
        ``^[A-Za-z0-9._/-]+$`` (no backslash, no NUL, no whitespace, no shell
        metacharacters);
      * must be relative — no leading ``/`` (absolute) and no leading ``-``
        (which a tool could treat as an option);
      * must contain no ``..`` path segment (no parent-directory traversal);
      * must not be a bare ``.`` or end with ``/`` (not a concrete file).

    Returns the path unchanged on success.
    """
    if not isinstance(path, str) or not path.strip():
        raise IdeRequestError(f"field {field} must be a non-empty string")
    value = path.strip()
    if not _REPO_RELATIVE_PATH_RE.match(value):
        raise IdeRequestError(
            f"field {field} must match ^[A-Za-z0-9._/-]{{1,256}}$ "
            "(conservative repo-relative path)"
        )
    if value.startswith("/"):
        raise IdeRequestError(f"field {field} must be relative (no leading '/')")
    if value.startswith("-"):
        raise IdeRequestError(f"field {field} must not start with '-'")
    if value.endswith("/"):
        raise IdeRequestError(f"field {field} must reference a file, not a directory")
    segments = value.split("/")
    if any(seg == ".." for seg in segments):
        raise IdeRequestError(f"field {field} must not contain a '..' segment")
    if value == ".":
        raise IdeRequestError(f"field {field} must reference a file, not '.'")
    return value


def _normalise_idempotency_key(raw: str) -> str:
    """Validate a client idempotency key and return the namespaced task_id.

    The returned value is ALWAYS within the ``ide-`` namespace so a client
    override can never collide with a Jira ``sonar_hash`` or a ``quota#<actor>#
    <window>`` counter item on the shared table, and can never be used to read
    or wedge another tenant's row via the status API's by-partition-key read
    (review finding #3).

    Rules (raises ``IdeRequestError`` on any violation):
      * must be a non-empty string;
      * a leading ``ide-`` is accepted (idempotent) but not required — the
        prefix is enforced on the returned value either way;
      * the remainder (after any ``ide-``) must match
        ``^[A-Za-z0-9._-]{1,128}$`` — bounded length, conservative charset, no
        ``#`` (so it cannot enter the ``quota#`` namespace), no whitespace, no
        path separators beyond ``.-_``;
      * the final value must not begin with a reserved prefix (e.g. ``quota#``);
        this is belt-and-braces since the charset already excludes ``#``.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise IdeRequestError("field idempotency_key must be a non-empty string")
    key = raw.strip()
    # Accept (but do not require) a caller-supplied ``ide-`` prefix so a client
    # that echoes a previously-returned task_id stays idempotent.
    body = key[len(IDE_TASK_ID_PREFIX):] if key.startswith(IDE_TASK_ID_PREFIX) else key
    if not _IDEMPOTENCY_KEY_RE.match(body):
        raise IdeRequestError(
            "field idempotency_key must match "
            "^[A-Za-z0-9._-]{1,128}$ (optionally prefixed 'ide-')"
        )
    task_id = f"{IDE_TASK_ID_PREFIX}{body}"
    for reserved in _RESERVED_KEY_PREFIXES:
        if task_id.startswith(reserved):
            raise IdeRequestError(
                f"field idempotency_key must not use the reserved '{reserved}' namespace"
            )
    return task_id


def derive_task_id(actor: str, repo_clone_url: str, base_branch: str,
                   description: str) -> str:
    """Derive a stable logical task_id for an IDE task (ADR-004).

    A sha256 over ``actor\\nrepo_clone_url\\nbase_branch\\ndescription``,
    truncated and prefixed with ``ide-``. Stable for the same inputs so a
    double-click / dropped-response re-dispatch is idempotent (the derived id
    is written into the physical ``sonar_hash`` partition-key attribute by the
    handler, so the claim/lease logic is reused unchanged).
    """
    material = "\n".join([
        actor or "",
        repo_clone_url or "",
        base_branch or "",
        description or "",
    ])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"ide-{digest[:32]}"


def _require_str(body: dict, field: str) -> str:
    """Return a required non-empty string field from the IDE request body."""
    value = body.get(field)
    if value is None:
        raise IdeRequestError(f"missing required field: {field}")
    if not isinstance(value, str):
        raise IdeRequestError(f"field {field} must be a string")
    value = value.strip()
    if not value:
        raise IdeRequestError(f"field {field} must not be empty")
    return value


def parse_ide_request(body: dict, actor: str) -> dict:
    """Validate and map an IDE task request into TaskPayload fields.

    ``body`` is the decoded JSON request from the IDE extension; ``actor`` is the
    authenticated developer identity (from the verified JWT claims, ADR-001).

    ``task_type`` (ADR-002 Option C / ADR-005) selects the worker shape and
    defaults to ``"one_shot"`` when absent:

      * ``"one_shot"`` — a single scoped instruction mapped ~1:1 onto the
        deterministic pipeline. ``description`` and ``summary`` are REQUIRED
        (the exact pre-existing behaviour).
      * ``"spec"`` — a whole ``tasks.md`` offloaded to the spec-executor worker.
        At least one of ``tasks_md_path`` (a traversal-safe repo-relative path)
        or ``tasks_md`` (inline content) is REQUIRED; ``description`` becomes
        OPTIONAL (the tasks.md is the work). ``summary`` is still required
        (used for the single PR title, ADR-006).

    Required request fields (always):
      * ``repo_clone_url`` — validated with ``validate_clone_url`` unchanged.
      * ``summary`` — a short PR/commit title.

    Required for ``one_shot`` only:
      * ``description`` — the natural-language instruction (= kiro-cli brief).

    Required for ``spec`` only (at least one of):
      * ``tasks_md_path`` — repo-relative path to the tasks.md, validated with
        ``validate_repo_relative_path`` (traversal-safe).
      * ``tasks_md`` — inline tasks.md content.

    Optional request fields:
      * ``base_branch`` — defaults to ``"main"``; validated.
      * ``file_to_fix`` — target file/area hint (pass-through, not shell-interp).
      * ``fix_branch`` — caller-supplied; validated. When absent, derived from
        the task_id as ``kiro/ide-<task_id-fragment>`` and validated.
      * ``idempotency_key`` — optional client override for the task_id (ADR-004).

    Returns a dict with keys: ``task_id``, ``source``, ``actor``,
    ``repo_clone_url``, ``base_branch``, ``fix_branch``, ``file_to_fix``,
    ``description``, ``summary``, ``acceptance``, ``task_type``, and (for a spec
    job, else ``None``) ``tasks_md_path`` / ``tasks_md``. Raises
    ``IdeRequestError`` on any missing/invalid field (the handler routes the
    reason through redact() and returns 400).
    """
    if body is None or not isinstance(body, dict):
        raise IdeRequestError("request body must be a JSON object")
    if not actor:
        raise IdeRequestError("missing actor identity")

    # Task-type discriminator: default to one_shot so existing requests are
    # unchanged. Validate against the known set.
    task_type = body.get("task_type")
    if task_type is None:
        task_type = TASK_TYPE_ONE_SHOT
    if not isinstance(task_type, str):
        raise IdeRequestError("field task_type must be a string")
    task_type = task_type.strip() or TASK_TYPE_ONE_SHOT
    if task_type not in TASK_TYPES:
        raise IdeRequestError(
            f"field task_type must be one of {sorted(TASK_TYPES)}"
        )

    repo_clone_url = _require_str(body, "repo_clone_url")
    validate_clone_url(repo_clone_url)

    summary = _require_str(body, "summary")

    # Spec-tasks source fields (spec jobs only).
    tasks_md_path = None
    tasks_md = None

    if task_type == TASK_TYPE_SPEC:
        # A spec job identifies the whole tasks.md, not a single instruction.
        # description is OPTIONAL; accept it when a non-empty string is given.
        raw_description = body.get("description")
        if raw_description is None:
            description = ""
        elif not isinstance(raw_description, str):
            raise IdeRequestError("field description must be a string")
        else:
            description = raw_description.strip()

        raw_path = body.get("tasks_md_path")
        if raw_path is not None:
            tasks_md_path = validate_repo_relative_path(raw_path)

        raw_inline = body.get("tasks_md")
        if raw_inline is not None:
            if not isinstance(raw_inline, str) or not raw_inline.strip():
                raise IdeRequestError("field tasks_md must be a non-empty string")
            tasks_md = raw_inline

        if tasks_md_path is None and tasks_md is None:
            raise IdeRequestError(
                "a spec task requires at least one of tasks_md_path or tasks_md"
            )
    else:
        # one_shot: EXACT pre-existing required-field semantics.
        description = _require_str(body, "description")

    base_branch = body.get("base_branch") or "main"
    if not isinstance(base_branch, str):
        raise IdeRequestError("field base_branch must be a string")
    base_branch = base_branch.strip() or "main"
    validate_branch_name(base_branch, field="base_branch")

    file_to_fix = body.get("file_to_fix")
    if file_to_fix is not None and not isinstance(file_to_fix, str):
        raise IdeRequestError("field file_to_fix must be a string")

    # Derive a stable logical task_id (ADR-004). A client-supplied idempotency
    # key, if present, overrides the server derivation — but it is constrained
    # to the ``ide-`` namespace with a bounded charset/length so it can never
    # collide with a Jira ``sonar_hash`` or a ``quota#...`` counter item on the
    # shared table, nor be used to read/wedge another tenant's row (finding #3).
    idempotency_key = body.get("idempotency_key")
    if idempotency_key is not None:
        task_id = _normalise_idempotency_key(idempotency_key)
    else:
        task_id = derive_task_id(actor, repo_clone_url, base_branch, description)

    fix_branch = body.get("fix_branch")
    if fix_branch is not None:
        if not isinstance(fix_branch, str) or not fix_branch.strip():
            raise IdeRequestError("field fix_branch must be a non-empty string")
        fix_branch = fix_branch.strip()
    else:
        # Derive a fix branch from the task_id and validate it. The task_id
        # always carries the ``ide-`` prefix, so strip it before re-prefixing to
        # avoid a redundant ``kiro/ide-ide-...`` branch name.
        task_id_fragment = task_id[len(IDE_TASK_ID_PREFIX):] if task_id.startswith(
            IDE_TASK_ID_PREFIX) else task_id
        fix_branch = f"kiro/ide-{task_id_fragment[:12]}"
    validate_branch_name(fix_branch, field="fix_branch")

    return {
        "task_id": task_id,
        "source": "kiro-ide",
        "actor": actor,
        "repo_clone_url": repo_clone_url,
        "base_branch": base_branch,
        "fix_branch": fix_branch,
        "file_to_fix": file_to_fix,
        "description": description,
        "summary": summary,
        "acceptance": IDE_ACCEPTANCE,
        "task_type": task_type,
        # Spec-only source fields; None for a one_shot job so the one-shot
        # payload is unchanged.
        "tasks_md_path": tasks_md_path,
        "tasks_md": tasks_md,
    }
