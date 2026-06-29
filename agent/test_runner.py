"""Auto-detect and run a repo's test suite."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

TEST_TIMEOUT_SECONDS = 600


def _run_cmd(cmd: list[str], cwd: Path) -> dict:
    """Run a test command, returning {success, output, skipped:False}."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        return {
            "success": False,
            "output": f"tests timed out after {TEST_TIMEOUT_SECONDS}s\n{out}",
            "skipped": False,
        }
    output = (proc.stdout or "") + (proc.stderr or "")
    return {"success": proc.returncode == 0, "output": output, "skipped": False}


def _package_json_has_test(repo_path: Path) -> bool:
    pkg = repo_path / "package.json"
    if not pkg.is_file():
        return False
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    scripts = data.get("scripts") or {}
    return bool(scripts.get("test"))


def _pyproject_uses_pytest(repo_path: Path) -> bool:
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return "pytest" in text


def _makefile_has_test(repo_path: Path) -> bool:
    mk = repo_path / "Makefile"
    if not mk.is_file():
        return False
    try:
        for line in mk.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("test:") or stripped == "test:":
                return True
    except OSError:
        return False
    return False


def detect_and_run(repo_path: str | Path) -> dict:
    """Auto-detect the test command for a repo and run it.

    Detection order:
      - package.json with a "test" script -> npm test
      - pom.xml                            -> mvn test -q
      - pytest.ini OR pyproject.toml(pytest) -> pytest
      - Makefile with a test target        -> make test
      - none found                          -> {"success": True, "skipped": True}

    Returns ``{"success": bool, "output": str, "skipped": bool}``.
    """
    repo_path = Path(repo_path)

    if _package_json_has_test(repo_path):
        return _run_cmd(["npm", "test"], repo_path)

    if (repo_path / "pom.xml").is_file():
        return _run_cmd(["mvn", "test", "-q"], repo_path)

    if (repo_path / "pytest.ini").is_file() or _pyproject_uses_pytest(repo_path):
        return _run_cmd(["pytest"], repo_path)

    if _makefile_has_test(repo_path):
        return _run_cmd(["make", "test"], repo_path)

    return {"success": True, "output": "no test runner detected", "skipped": True}
