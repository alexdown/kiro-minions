"""Run kiro-cli headless against a cloned repo."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

KIRO_TIMEOUT_SECONDS = 600


def run_kiro(repo_path: str | Path, description: str, file_to_fix: str | None) -> dict:
    """Run kiro-cli headless with the full ticket description as the brief.

    The full Jira description is written to a temp file and piped via stdin;
    the CLI prompt points kiro at the target file.

    Returns ``{"success": bool, "output": str}``.
    """
    repo_path = Path(repo_path)
    target = file_to_fix or "(see description)"
    prompt = (
        "Fix the SonarQube finding described below. "
        f"Target file: {target}"
    )

    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="kiro-desc-", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(description or "")
        tmp.flush()
        tmp.close()

        with open(tmp.name, "r", encoding="utf-8") as stdin_file:
            try:
                proc = subprocess.run(
                    [
                        "kiro-cli",
                        "chat",
                        "--no-interactive",
                        "--trust-all-tools",
                        prompt,
                    ],
                    cwd=str(repo_path),
                    stdin=stdin_file,
                    capture_output=True,
                    text=True,
                    timeout=KIRO_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                out = (exc.stdout or "") + (exc.stderr or "")
                if isinstance(out, bytes):
                    out = out.decode("utf-8", errors="replace")
                return {
                    "success": False,
                    "output": f"kiro-cli timed out after {KIRO_TIMEOUT_SECONDS}s\n{out}",
                }

        output = (proc.stdout or "") + (proc.stderr or "")
        return {"success": proc.returncode == 0, "output": output}
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except OSError:
            pass
