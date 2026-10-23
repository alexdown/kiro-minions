"""Test doubles for kiro-minions, stdlib-only.

`FakeRunner` is a drop-in replacement for `agent.RUNNER`. It records every
call (cmd/argv, cwd, check, timeout, env) and returns scripted results so tests
can deterministically simulate kiro-cli, git, gh and test-runner outcomes
without spawning any real subprocess.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


@dataclass
class CallRecord:
    """A single recorded invocation of the runner."""

    cmd: str | list[str]
    cwd: object = None
    check: bool = True
    timeout: int | None = None
    env: dict | None = None

    @property
    def display(self) -> str:
        """Human-readable command string (mirrors agent._default_runner)."""
        if isinstance(self.cmd, (list, tuple)):
            return " ".join(str(part) for part in self.cmd)
        return str(self.cmd)


@dataclass
class ScriptedResult:
    """A scripted outcome for a runner call.

    If `match` is set, the result only applies to commands whose display string
    contains that substring. If `match` is None the result is part of the
    ordered fallback queue consumed in sequence.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    match: str | None = None


class FakeRunner:
    """Callable stand-in for ``agent.RUNNER``.

    Usage::

        runner = FakeRunner(
            results=[ScriptedResult(stdout="https://github.com/o/r/pull/1",
                                    match="gh pr create")],
        )
        agent.RUNNER = runner

    Matching strategy for each call:
      1. First unconsumed substring-matched ``ScriptedResult`` whose ``match``
         appears in the command display string.
      2. Otherwise the next item from the ordered fallback queue (results with
         ``match is None``).
      3. Otherwise a default success (rc=0, empty stdout/stderr).

    When the chosen result has a non-zero returncode and ``check=True`` the
    runner raises ``RuntimeError`` mirroring ``agent._default_runner`` so that
    retry / failure paths are exercised faithfully.
    """

    def __init__(self, results=None, env=None):
        self.calls: list[CallRecord] = []
        self._matchers: list[ScriptedResult] = []
        self._queue: list[ScriptedResult] = []
        self._env = dict(env) if env else None
        for res in results or []:
            if isinstance(res, ScriptedResult):
                scripted = res
            elif isinstance(res, dict):
                scripted = ScriptedResult(**res)
            elif isinstance(res, (tuple, list)):
                scripted = ScriptedResult(*res)
            else:
                raise TypeError(f"unsupported scripted result: {res!r}")
            if scripted.match is None:
                self._queue.append(scripted)
            else:
                self._matchers.append(scripted)

    def __call__(self, cmd, cwd=None, check=True, timeout=600):
        record = CallRecord(
            cmd=cmd, cwd=cwd, check=check, timeout=timeout, env=self._env,
        )
        self.calls.append(record)

        scripted = self._select(record.display)

        if check and scripted.returncode != 0:
            raise RuntimeError(
                f"Command failed (rc={scripted.returncode}): {record.display}\n"
                f"stderr: {scripted.stderr[-500:]}"
            )

        return subprocess.CompletedProcess(
            args=cmd,
            returncode=scripted.returncode,
            stdout=scripted.stdout,
            stderr=scripted.stderr,
        )

    def _select(self, display: str) -> ScriptedResult:
        for i, scripted in enumerate(self._matchers):
            if scripted.match in display:
                return self._matchers.pop(i)
        if self._queue:
            return self._queue.pop(0)
        return ScriptedResult()

    # -- convenience accessors for assertions -------------------------------

    @property
    def commands(self) -> list[str]:
        """Display strings for every recorded call, in order."""
        return [c.display for c in self.calls]

    def find(self, substring: str) -> CallRecord | None:
        """Return the first recorded call whose display contains ``substring``."""
        for call in self.calls:
            if substring in call.display:
                return call
        return None

    def count(self, substring: str) -> int:
        """Number of recorded calls whose display contains ``substring``."""
        return sum(1 for call in self.calls if substring in call.display)
