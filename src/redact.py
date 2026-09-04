#!/usr/bin/env python3
"""Defensive redaction of credential-shaped text.

Everything this package writes to a log, an artifact, or an MCP response is passed
through :func:`redact` first.

The same rules serve two jobs with opposite tolerances, which is worth stating because
getting it wrong once already cost a blocked commit. Masking a log line is cheap, so
these lean broad. But :func:`scan_for_secrets` also gates ``duet_finalize``, where a
match refuses the commit outright -- there a false positive is a wall, not a smudge.
The rules are therefore keyed on the shape of the *value*: a credential looks like a
credential wherever it appears, whereas a variable merely *named* like one is ordinary
code that must stay committable.
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

#: Each rule is ``(label, pattern)``. The label is what a refusal reports, so it has to
#: read as a reason a person can act on: "line 437 looks like an inline credential
#: assignment" sends them to the line, where "pattern#11 at line 437" sends them to us.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Provider-specific shapes first: they are the highest-confidence matches. Each one
    # identifies a credential by the shape of the *value*, which is why they can afford
    # to be strict -- and why the generic rule at the end can afford to be conservative.
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("a GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("an OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("an Anthropic-style API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("a Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b")),
    (
        "a JSON web token",
        re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    # PEM private key blocks, collapsed whole.
    (
        "a PEM private key block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Credentials embedded in URLs: keep the scheme and host, drop the userinfo.
    ("a password in a URL", re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")),
    # Generic "NAME=value" / "NAME: value" assignments for secret-shaped names.
    #
    # Both halves are deliberately narrow, because this is the only rule that judges a
    # value by the name next to it rather than by its own shape, and it is the only one
    # that can plausibly fire on source code.
    #
    # The *name* must be a whole word, optionally joined by ``_`` or ``-`` to other
    # words. An earlier version allowed any surrounding letters, so ``$tokens`` in a
    # parser and ``password_field`` in a form model both read as credentials -- and
    # since a match refuses the commit outright, that made agent_duet unable to publish
    # any repository containing a lexer. Observed on 2026-09-04: ``$tokens = $null``
    # blocked finalize on a PowerShell installer.
    #
    # The *value* must look like a credential too, and that is the harder half, because
    # identifiers are spelled from the same alphabet as tokens. Two branches:
    #
    # A *quoted* literal is taken at face value once it is long enough. Code does not
    # normally assign an opaque string to something named ``api_key`` unless it is one.
    #
    # A *bare* value has to earn it, because bare is where real code lives. It must
    # carry a digit or a base64 character -- ``get_credentials``, ``re.compile`` and
    # ``Optional`` do not -- and it must be the whole value, not a prefix of a larger
    # expression. That trailing lookahead is what rejects ``self.next_token()`` and
    # ``Optional[str]``: it refuses to stop in the middle of a call or a subscript, and
    # because it also covers the value alphabet the engine cannot backtrack into a
    # shorter prefix to sneak past it.
    (
        "an inline credential assignment",
        re.compile(
            r"(?i)\b((?:[A-Z0-9]+[_-])*"
            r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY"
            r"|PRIVATE[_-]?KEY|CREDENTIALS?|BEARER|PAT)"
            r"(?:[_-][A-Z0-9]+)*)"
            r"(\s*[:=]\s*)"
            r"(?!(?:null|none|nil|true|false|undefined|changeme|redacted|example"
            r"|placeholder|your[_-]?\w+|xxx+)[\s,;)\]}]*$)"
            r"(\"[^\"\n]{8,}\"|'[^'\n]{8,}'"
            r"|(?=[A-Za-z0-9+/=_.~-]*[0-9+/=])[A-Za-z0-9+/=_.~-]{8,}"
            r"(?![(\[A-Za-z0-9+/=_.~-]))"
        ),
    ),
    # "Authorization: Bearer ..." headers.
    (
        "an Authorization header",
        re.compile(r"(?i)\b(authorization\s*:\s*)(bearer|basic|token)\s+\S+"),
    ),
)


def redact(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced by :data:`MASK`."""
    if not text:
        return text
    out = text
    for _label, pattern in _PATTERNS:
        if pattern.groups == 3:  # NAME=value: keep the name, mask only the value.
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
        elif pattern.groups == 2:  # Authorization header: keep the scheme word.
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)} {MASK}", out)
        else:
            out = pattern.sub(MASK, out)
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


def scan_for_secrets(text: str, *, line_offset: int = 0) -> list[str]:
    """Return short descriptions of credential-shaped matches found in ``text``.

    Used to refuse a commit or to flag a reviewer report. The matched text itself is
    never returned -- only which rule fired and where -- so callers can log the result
    freely. ``line_offset`` is the number of lines preceding ``text`` in its file, so a
    scanner reading a large file in chunks still reports a line number a person can open.
    """
    findings: list[str] = []
    for label, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            line = line_offset + text.count("\n", 0, match.start()) + 1
            findings.append(f"line {line} looks like {label}")
            break  # One report per rule is enough to justify refusal.
    return findings


def redact_iter(lines: Iterable[str]) -> list[str]:
    """Redact each line of an iterable."""
    return [redact(line) for line in lines]
