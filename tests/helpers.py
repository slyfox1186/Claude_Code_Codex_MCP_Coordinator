#!/usr/bin/env python3
"""Small helpers shared by the test modules."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Deterministic stand-ins for the real CLIs.
FIXTURE_BIN = Path(__file__).parent / "fixtures" / "bin"


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd``, raising on failure."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def head_sha(path: Path) -> str:
    """Return the exact HEAD sha of a repository."""
    return git("rev-parse", "HEAD", cwd=path).stdout.strip()
