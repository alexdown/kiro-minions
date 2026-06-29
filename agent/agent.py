"""AgentCore coding-agent entry point.

Receives a TicketPayload as JSON input (from the AgentCore managed harness),
then orchestrates: git clone -> create branch -> run kiro -> run tests ->
open PR. Returns ``{"status", "pr_url", "error"}``.
"""

from __future__ import annotations

import json
import os
import tempfile

from git_ops import GitError, clone_repo, commit_and_push, create_branch
from github_client import create_pr
from kiro_runner import run_kiro
from test_runner import detect_and_run


def _coerce_payload(event) -> dict:
    """Normalize AgentCore input into a TicketPayload dict.

    AgentCore may deliver the payload as a dict, a JSON string, or wrapped under
    common keys (``inputText`` / ``body`` / ``payload``).
    """
    if event is None:
        raise ValueError("empty input event")

    if isinstance(event, (bytes, bytearray)):
        event = event.decode("utf-8")

    if isinstance(event, str):
        return json.loads(event)

    if isinstance(event, dict):
        for key in ("payload", "inputText", "body", "input"):
            if key in event and event[key] is not None:
                inner = event[key]
                if isinstance(inner, (str, bytes, bytearray)):
                    return json.loads(
                        inner.decode("utf-8") if isinstance(inner, (bytes, bytearray))
                        else inner
                    )
                if isinstance(inner, dict):
                    return inner
        # Already a bare payload.
        if "repo_clone_url" in event:
            return event

    raise ValueError(f"could not extract TicketPayload from input: {type(event)!r}")


def _result(status: str, pr_url: str | None, error: str | None) -> dict:
    return {"status": status, "pr_url": pr_url, "error": error}


def run(payload: dict) -> dict:
    """Execute the full remediation flow for one TicketPayload."""
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        return _result("failure", None, "GITHUB_TOKEN env var is not set")

    # kiro-cli reads KIRO_API_KEY from the environment directly; just validate.
    if not os.environ.get("KIRO_API_KEY"):
        return _result("failure", None, "KIRO_API_KEY env var is not set")

    try:
        repo_clone_url = payload["repo_clone_url"]
        base_branch = payload.get("base_branch") or "main"
        fix_branch = payload.get("fix_branch")
        file_to_fix = payload.get("file_to_fix")
        description = payload.get("description") or ""
        ticket_id = payload.get("ticket_id") or ""
        ticket_url = payload.get("ticket_url") or ""
        summary = payload.get("summary") or "SonarQube remediation"
        sonar_hash = payload.get("sonar_hash") or ""
    except KeyError as exc:
        return _result("failure", None, f"missing required payload field: {exc}")

    if not repo_clone_url:
        return _result("failure", None, "repo_clone_url is required")
    if not fix_branch:
        return _result("failure", None, "fix_branch is required")

    with tempfile.TemporaryDirectory(prefix="kiro-minions-") as workdir:
        # 1. Clone + branch
        try:
            repo_path = clone_repo(repo_clone_url, github_token, workdir)
            create_branch(repo_path, base_branch, fix_branch)
        except GitError as exc:
            return _result("failure", None, f"git setup failed: {exc}")

        # 2. Run kiro headless
        kiro_result = run_kiro(repo_path, description, file_to_fix)
        if not kiro_result["success"]:
            return _result(
                "failure", None, f"kiro-cli failed: {kiro_result['output'][:2000]}"
            )

        # 3. Run tests
        test_result = detect_and_run(repo_path)
        if not test_result["success"]:
            return _result(
                "failure", None, f"tests failed: {test_result['output'][:2000]}"
            )

        # 4. Commit + push
        commit_message = f"fix: {summary} [{ticket_id}]\n\nsonar_hash: {sonar_hash}"
        try:
            pushed = commit_and_push(
                repo_path, fix_branch, github_token, commit_message
            )
        except GitError as exc:
            return _result("failure", None, f"push failed: {exc}")

        if not pushed:
            return _result(
                "failure", None, "kiro produced no changes; nothing to commit"
            )

        # 5. Open PR
        try:
            pr_url = create_pr(
                token=github_token,
                repo_clone_url=repo_clone_url,
                base_branch=base_branch,
                fix_branch=fix_branch,
                ticket_id=ticket_id,
                ticket_url=ticket_url,
                summary=summary,
                sonar_hash=sonar_hash,
            )
        except Exception as exc:  # noqa: BLE001
            return _result("failure", None, f"PR creation failed: {exc}")

    return _result("success", pr_url, None)


def handler(event, context=None):  # noqa: ANN001 - harness signature
    """Entry point for the AgentCore managed harness."""
    try:
        payload = _coerce_payload(event)
    except (ValueError, json.JSONDecodeError) as exc:
        return _result("failure", None, f"invalid input payload: {exc}")
    return run(payload)


def main() -> None:
    """CLI / container entry point: read JSON payload from stdin."""
    import sys

    raw = sys.stdin.read()
    result = handler(raw, None)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
