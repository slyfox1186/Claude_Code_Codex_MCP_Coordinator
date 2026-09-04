#!/usr/bin/env python3
"""Defensive redaction of credential-shaped text.

Everything this package writes to a log, an artifact, or an MCP response is passed
through :func:`redact` first. The patterns are deliberately broad: a false positive
costs a few unreadable characters, a false negative leaks a token.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

MASK = "[REDACTED]"

# Environment variable names whose *values* must never be written anywhere.
SECRET_ENV_NAME_PATTERN = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|ACCESS_KEY|PRIVATE_KEY"
    r"|CREDENTIAL|CREDENTIALS|SESSION|COOKIE|AUTH|BEARER|PAT)(?:$|_)",
    re.IGNORECASE,
)

#: Names that match the pattern above but hold a path or a socket, not a credential.
#: Dropping these would break ssh agent forwarding and git signing for no benefit.
SAFE_ENV_NAMES: frozenset[str] = frozenset(
    {
        "SSH_AUTH_SOCK",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_SESSION_TYPE",
        "XDG_SESSION_CLASS",
        "XDG_SESSION_ID",
        "XDG_SESSION_DESKTOP",
        "GPG_AGENT_INFO",
    }
)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Provider-specific shapes first: they are the highest-confidence matches.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),  # GitHub PAT / OAuth / refresh
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),  # OpenAI style
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),  # Anthropic style
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),  # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),  # GitLab
    re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    # PEM private key blocks, collapsed whole.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Credentials embedded in URLs: keep the scheme and host, drop the userinfo.
    re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)"),
    # Generic "NAME=value" / "NAME: value" assignments for secret-shaped names.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY"
        r"|PRIVATE[_-]?KEY|CREDENTIALS?|BEARER)[A-Z0-9_]*)"
        r"(\s*[:=]\s*)"
        r"(\"[^\"\n]+\"|'[^'\n]+'|[^\s,;)\]}]+)"
    ),
    # "Authorization: Bearer ..." headers.
    re.compile(r"(?i)\b(authorization\s*:\s*)(bearer|basic|token)\s+\S+"),
)


def redact(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced by :data:`MASK`."""
    if not text:
        return text
    out = text
    for index, pattern in enumerate(_PATTERNS):
        if pattern.groups == 3:  # NAME=value: keep the name, mask only the value.
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
        elif pattern.groups == 2:  # Authorization header: keep the scheme word.
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)} {MASK}", out)
        else:
            out = pattern.sub(MASK, out)
        del index
    return out


def redact_bytes(chunk: bytes) -> bytes:
    """Redact a byte chunk, tolerating partial/invalid UTF-8 from a child stream."""
    return redact(chunk.decode("utf-8", errors="replace")).encode("utf-8")


def secret_env_names(env: Mapping[str, str]) -> list[str]:
    """Return the names in ``env`` whose values are considered secret."""
    return sorted(
        name
        for name in env
        if name.upper() not in SAFE_ENV_NAMES and SECRET_ENV_NAME_PATTERN.search(name)
    )


def scan_for_secrets(text: str) -> list[str]:
    """Return short descriptions of credential-shaped matches found in ``text``.

    Used to refuse a commit or to flag a reviewer report; the matched text itself is
    never returned, only the pattern index and a location, so callers can log freely.
    """
    findings: list[str] = []
    for index, pattern in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"pattern#{index} at line {line}")
            break  # One report per pattern is enough to justify refusal.
    return findings


def redact_iter(lines: Iterable[str]) -> list[str]:
    """Redact each line of an iterable."""
    return [redact(line) for line in lines]
