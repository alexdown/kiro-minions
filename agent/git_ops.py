"""Git operations for the coding agent.

All clone/push use HTTPS with a token embedded in the URL:
``https://x-token:{token}@github.com/...``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class GitError(RuntimeError):
    """Raised when a git command fails."""


def _run(args: list[str], cwd: str | Path | None = None,
         env: dict | None = None) -> str:
    """Run a git command, raising GitError on non-zero exit.

    Returns captured stdout. Token-bearing URLs are scrubbed from error output.
    """
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise GitError(f"git {' '.join(args[1:])} failed: {msg}")
    return proc.stdout


def _authenticated_url(clone_url: str, token: str) -> str:
    """Inject a token into an HTTPS clone URL.

    ``https://github.com/owner/repo.git`` ->
    ``https://x-token:<token>@github.com/owner/repo.git``
    """
    parts = urlsplit(clone_url)
    if parts.scheme != "https":
        raise GitError(f"only https clone URLs are supported, got: {parts.scheme!r}")
    # Strip any existing userinfo, then add our own.
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"x-token:{token}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _repo_dir_name(clone_url: str) -> str:
    """Derive the local directory name from a clone URL."""
    path = urlsplit(clone_url).path.rstrip("/")
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name or "repo"


def clone_repo(clone_url: str, token: str, workdir: str | Path) -> Path:
    """Clone ``clone_url`` into ``workdir`` using a token. Returns repo path."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    auth_url = _authenticated_url(clone_url, token)
    dest = workdir / _repo_dir_name(clone_url)
    _run(["git", "clone", auth_url, str(dest)])
    return dest


def create_branch(repo_path: str | Path, base_branch: str, fix_branch: str) -> None:
    """Check out ``base_branch`` then create/switch to ``fix_branch``."""
    repo_path = Path(repo_path)
    # Fetch the base branch explicitly in case it is not the default.
    _run(["git", "fetch", "origin", base_branch], cwd=repo_path)
    _run(["git", "checkout", base_branch], cwd=repo_path)
    # Create the fix branch from base. -B resets if it already exists locally.
    _run(["git", "checkout", "-B", fix_branch], cwd=repo_path)


def commit_and_push(repo_path: str | Path, fix_branch: str, token: str,
                    message: str) -> bool:
    """Stage all changes, commit, and push ``fix_branch`` to origin.

    Returns True if a commit was created and pushed, False if there was nothing
    to commit (clean tree).
    """
    repo_path = Path(repo_path)
    _run(["git", "add", "-A"], cwd=repo_path)

    # Detect staged changes; `git diff --cached --quiet` exits 1 when changes exist.
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_path),
        capture_output=True,
    )
    if status.returncode == 0:
        # No staged changes.
        return False

    _run(["git", "commit", "-m", message], cwd=repo_path)

    # Re-point origin to an authenticated URL so push works headless.
    origin_url = _run(["git", "remote", "get-url", "origin"], cwd=repo_path).strip()
    auth_url = _authenticated_url(origin_url, token)
    _run(["git", "remote", "set-url", "origin", auth_url], cwd=repo_path)
    _run(["git", "push", "--set-upstream", "origin", fix_branch], cwd=repo_path)
    return True
