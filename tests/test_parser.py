"""Unit tests for the orchestrator description parser.

Exercises the real PROJ-15 ticket content from the specs, plus edge cases.
"""

import os
import sys

import pytest

# Make orchestrator/ importable without packaging.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "orchestrator"),
)

from parser import (  # noqa: E402
    extract_sonar_hash,
    has_sonarqube_label,
    parse_description_header,
)


# The exact PROJ-15 description (Part 1 header + Part 2 inline SonarQube report).
SW15_DESCRIPTION = """\
Repo URL: https://github.com/your-org/your-repo
Clone: https://github.com/your-org/your-repo.git
Base branch: master
File to fix: server.js:135
Suggested fix branch: sonarqube-fix/xss-swig-autoescape

SONAR-MCP-001: XSS — Template Auto-Escaping Disabled
Priority: P0 (Critical) · Type: Security Vulnerability · CWE-79 · OWASP A03:2021
File: your-repo/server.js · Line: 135 · Rule: javascript:S5247
Hotspot Key: 6f50f92a-65f2-4c4c-aae5-9b069b44430a

Description / Impact / Recommended Fix:
  swig.setDefaults({ autoescape: true });

References · _sonar-hash: 6f50f92a-65f2-4c4c-aae5-9b069b44430a (dedup marker)
"""

# The real labels array from PROJ-15.
SW15_LABELS = [
    "CWE-79",
    "OWASP-A03",
    "P0",
    "SONARQUBE-FIX",
    "XSS",
    "security",
    "sonar-hash:6f50f92a-65f2-4c4c-aae5-9b069b44430a",
    "sonarqube",
]


def test_parse_sw15_header():
    header = parse_description_header(SW15_DESCRIPTION)
    assert header["repo_clone_url"] == "https://github.com/your-org/your-repo.git"
    assert header["base_branch"] == "master"
    assert header["file_to_fix"] == "server.js:135"
    assert header["fix_branch"] == "sonarqube-fix/xss-swig-autoescape"


def test_extract_sonar_hash_sw15():
    assert extract_sonar_hash(SW15_LABELS) == (
        "6f50f92a-65f2-4c4c-aae5-9b069b44430a"
    )


def test_has_sonarqube_label_sw15():
    assert has_sonarqube_label(SW15_LABELS) is True


def test_parse_is_case_insensitive_on_keys():
    desc = (
        "REPO URL: https://github.com/x/y\n"
        "clone: https://github.com/x/y.git\n"
        "BASE BRANCH: develop\n"
        "FILE TO FIX: src/app.py:10\n"
        "Suggested Fix Branch: sonarqube-fix/foo\n"
    )
    header = parse_description_header(desc)
    assert header["repo_clone_url"] == "https://github.com/x/y.git"
    assert header["base_branch"] == "develop"
    assert header["file_to_fix"] == "src/app.py:10"
    assert header["fix_branch"] == "sonarqube-fix/foo"


def test_missing_clone_line_raises():
    desc = (
        "Repo URL: https://github.com/x/y\n"
        "Base branch: main\n"
        "File to fix: a.py:1\n"
    )
    with pytest.raises(ValueError):
        parse_description_header(desc)


def test_missing_base_branch_defaults_to_main():
    desc = (
        "Clone: https://github.com/x/y.git\n"
        "File to fix: a.py:1\n"
        "Suggested fix branch: sonarqube-fix/foo\n"
    )
    header = parse_description_header(desc)
    assert header["base_branch"] == "main"
    assert header["fix_branch"] == "sonarqube-fix/foo"


def test_non_github_clone_url_rejected():
    # After issue #4 strict validation, only https://github.com hosts are
    # accepted; a GitHub Enterprise host is rejected with a ValueError.
    desc = "Clone: https://ghe.example.com/org/repo.git\n"
    with pytest.raises(ValueError):
        parse_description_header(desc)


def test_extract_sonar_hash_missing_returns_none():
    assert extract_sonar_hash(["sonarqube", "P0"]) is None
    assert extract_sonar_hash([]) is None
    assert extract_sonar_hash(None) is None


def test_extract_sonar_hash_case_insensitive_prefix():
    labels = ["SONAR-HASH:abc-123"]
    assert extract_sonar_hash(labels) == "abc-123"


def test_has_sonarqube_label_accepts_plain_sonarqube():
    assert has_sonarqube_label(["sonarqube"]) is True
    assert has_sonarqube_label(["SONARQUBE-FIX"]) is True
    assert has_sonarqube_label(["security", "P0"]) is False
    assert has_sonarqube_label([]) is False


def test_unrecognized_lines_are_ignored():
    desc = (
        "Clone: https://github.com/x/y.git\n"
        "Some random note: ignore me\n"
        "Base branch: main\n"
    )
    header = parse_description_header(desc)
    assert header["repo_clone_url"] == "https://github.com/x/y.git"
    assert header["base_branch"] == "main"


def test_empty_clone_value_raises():
    desc = "Clone:   \nBase branch: main\n"
    with pytest.raises(ValueError):
        parse_description_header(desc)


def test_none_description_raises():
    with pytest.raises(ValueError):
        parse_description_header(None)


# ---------------------------------------------------------------------------
# Issue #4: strict validation of branch names and clone URLs
# ---------------------------------------------------------------------------

from parser import (  # noqa: E402
    validate_branch_name,
    validate_clone_url,
)


INJECTION_BRANCHES = [
    "main; rm -rf /",
    "--upload-pack=evil",
    "../../etc",
    "a b",
    "-malicious",
    "feature..secret",
]


@pytest.mark.parametrize("bad", INJECTION_BRANCHES)
def test_validate_branch_name_rejects_injection(bad):
    with pytest.raises(ValueError):
        validate_branch_name(bad)


def test_validate_branch_name_accepts_good():
    assert validate_branch_name("sonarqube-fix/xss-swig-autoescape") == (
        "sonarqube-fix/xss-swig-autoescape"
    )
    assert validate_branch_name("master") == "master"
    assert validate_branch_name("main") == "main"


BAD_CLONE_URLS = [
    "https://x-token:abc@github.com/o/r.git",
    "http://github.com/o/r",
    "https://evil.com/o/r.git",
    "https://github.com/o/r/extra",
    "git@github.com:o/r.git",
    "https://github.com/o",
    "https://github.com/o/r?x=1",
]


@pytest.mark.parametrize("bad", BAD_CLONE_URLS)
def test_validate_clone_url_rejects_bad(bad):
    with pytest.raises(ValueError):
        validate_clone_url(bad)


def test_validate_clone_url_accepts_github():
    assert validate_clone_url("https://github.com/your-org/your-repo") == (
        "https://github.com/your-org/your-repo"
    )
    assert validate_clone_url("https://github.com/your-org/your-repo.git") == (
        "https://github.com/your-org/your-repo.git"
    )


@pytest.mark.parametrize("bad_branch", INJECTION_BRANCHES)
def test_parse_header_rejects_injection_base_branch(bad_branch):
    desc = (
        "Clone: https://github.com/your-org/your-repo.git\n"
        f"Base branch: {bad_branch}\n"
    )
    with pytest.raises(ValueError):
        parse_description_header(desc)


@pytest.mark.parametrize("bad_branch", INJECTION_BRANCHES)
def test_parse_header_rejects_injection_fix_branch(bad_branch):
    desc = (
        "Clone: https://github.com/your-org/your-repo.git\n"
        "Base branch: master\n"
        f"Suggested fix branch: {bad_branch}\n"
    )
    with pytest.raises(ValueError):
        parse_description_header(desc)


@pytest.mark.parametrize("bad_url", BAD_CLONE_URLS)
def test_parse_header_rejects_bad_clone_url(bad_url):
    desc = f"Clone: {bad_url}\nBase branch: master\n"
    with pytest.raises(ValueError):
        parse_description_header(desc)


def test_parse_header_accepts_valid_proj15():
    header = parse_description_header(SW15_DESCRIPTION)
    assert header["repo_clone_url"] == "https://github.com/your-org/your-repo.git"
    assert header["base_branch"] == "master"
    assert header["fix_branch"] == "sonarqube-fix/xss-swig-autoescape"
