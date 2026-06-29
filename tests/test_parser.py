"""Unit tests for the orchestrator description parser.

Exercises the real SW-15 ticket content from the specs, plus edge cases.
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


# The exact SW-15 description (Part 1 header + Part 2 inline SonarQube report).
SW15_DESCRIPTION = """\
Repo URL: https://github.com/alexdown/NodeGoat
Clone: https://github.com/alexdown/NodeGoat.git
Base branch: master
File to fix: server.js:135
Suggested fix branch: sonarqube-fix/xss-swig-autoescape

SONAR-MCP-001: XSS — Template Auto-Escaping Disabled
Priority: P0 (Critical) · Type: Security Vulnerability · CWE-79 · OWASP A03:2021
File: NodeGoat/server.js · Line: 135 · Rule: javascript:S5247
Hotspot Key: 6f50f92a-65f2-4c4c-aae5-9b069b44430a

Description / Impact / Recommended Fix:
  swig.setDefaults({ autoescape: true });

References · _sonar-hash: 6f50f92a-65f2-4c4c-aae5-9b069b44430a (dedup marker)
"""

# The real labels array from SW-15.
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
    assert header["repo_clone_url"] == "https://github.com/alexdown/NodeGoat.git"
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


def test_clone_url_with_https_containing_port_in_path():
    # Clone value should be taken verbatim after the colon (only first ':' splits).
    desc = "Clone: https://ghe.example.com/org/repo.git\n"
    header = parse_description_header(desc)
    assert header["repo_clone_url"] == "https://ghe.example.com/org/repo.git"


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
