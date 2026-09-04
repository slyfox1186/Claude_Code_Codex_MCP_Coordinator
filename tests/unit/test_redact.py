#!/usr/bin/env python3
"""The redactor and the secret scanner."""

from __future__ import annotations

import pytest
from agent_duet.redact import MASK, redact, scan_for_secrets, secret_env_names


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "A" * 36,
        "github_pat_" + "B" * 30,
        "sk-" + "C" * 32,
        "sk-ant-" + "D" * 40,
        "xoxb-1234567890-abcdefghij",
        "AKIA" + "E" * 16,
        "AIza" + "F" * 35,
        "glpat-" + "G" * 20,
    ],
)
def test_provider_tokens_are_masked(secret):
    assert secret not in redact(f"the token is {secret} ok")
    assert MASK in redact(f"the token is {secret} ok")


def test_jwt_is_masked():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    assert jwt not in redact(jwt)


def test_pem_block_is_masked():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert "MIIabc" not in redact(pem)


def test_url_credentials_are_masked_but_host_survives():
    out = redact("https://user:hunter2@github.com/org/repo.git")
    assert "hunter2" not in out
    assert "github.com/org/repo.git" in out


def test_assignment_keeps_the_name_and_drops_the_value():
    out = redact('MY_API_KEY = "supersecretvalue"')
    assert "MY_API_KEY" in out
    assert "supersecretvalue" not in out


def test_authorization_header_is_masked():
    out = redact("Authorization: Bearer abcdef123456")
    assert "abcdef123456" not in out
    assert "Bearer" in out


def test_ordinary_text_is_untouched():
    text = "def add(a, b):\n    return a + b\n"
    assert redact(text) == text


def test_empty_input():
    assert redact("") == ""


def test_secret_env_names_finds_likely_tokens():
    names = secret_env_names(
        {
            "GITHUB_TOKEN": "x",
            "AWS_SECRET_ACCESS_KEY": "x",
            "MY_PASSWORD": "x",
            "SESSION_ID": "x",
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "LANG": "C",
        }
    )
    assert "GITHUB_TOKEN" in names
    assert "AWS_SECRET_ACCESS_KEY" in names
    assert "MY_PASSWORD" in names
    assert "PATH" not in names
    assert "HOME" not in names


def test_scan_reports_findings_without_echoing_them():
    findings = scan_for_secrets("token: ghp_" + "A" * 36)
    assert findings
    assert all("ghp_" not in item for item in findings)


def test_scan_is_quiet_on_clean_text():
    assert scan_for_secrets("just some ordinary prose about tokens of appreciation") == []
