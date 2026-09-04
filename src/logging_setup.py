#!/usr/bin/env python3
"""Verbose, file-backed logging.

An MCP client usually swallows a stdio server's stderr, so stderr alone is useless for
diagnosis. Every process this package starts therefore writes a rotating, redacted log
file under the state directory as well, and `agent-duet logs` prints all of it back.

Stdout is never touched: it carries JSON-RPC.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from .redact import redact

#: Set AGENT_DUET_LOG_LEVEL=DEBUG|INFO|WARNING to override the configured level.
LEVEL_ENV_VAR = "AGENT_DUET_LOG_LEVEL"
DEFAULT_LEVEL = "DEBUG"

_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s [%(name)s] %(process)d %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


class RedactingFormatter(logging.Formatter):
    """Formatter that runs every finished record through the credential filter."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def default_state_dir() -> Path:
    """Return the state directory without needing a parsed config.

    Startup failures (a missing or invalid config) are exactly when logs matter most, so
    this must not depend on the config being loadable.
    """
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "agent-duet"


def log_dir(state_dir: Path | None = None) -> Path:
    """Return (and create) the directory holding process logs."""
    directory = (state_dir or default_state_dir()) / "logs"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):  # odd mounts may refuse chmod
        directory.chmod(0o700)
    return directory


def resolve_level(configured: str | None = None) -> int:
    """Resolve the effective level: environment beats config beats the default."""
    name = (os.environ.get(LEVEL_ENV_VAR) or configured or DEFAULT_LEVEL).upper()
    return getattr(logging, name, logging.DEBUG)


def setup_logging(
    role: str,
    *,
    state_dir: Path | None = None,
    level: str | None = None,
    to_stderr: bool = True,
) -> Path:
    """Configure root logging for one process and return its log file path.

    ``role`` becomes part of the filename ("server", "worker", "doctor", ...) so the two
    sides of a run are easy to tell apart in one directory.
    """
    global _configured
    directory = log_dir(state_dir)
    path = directory / f"{role}.log"

    root = logging.getLogger()
    if _configured:
        return path
    root.setLevel(resolve_level(level))

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=20_000_000, backupCount=3, encoding="utf-8", delay=False
    )
    handler.setFormatter(RedactingFormatter(_FORMAT, datefmt=_DATEFMT))
    root.addHandler(handler)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)

    if to_stderr:
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(RedactingFormatter(_FORMAT, datefmt=_DATEFMT))
        root.addHandler(stream)

    # The MCP SDK and anyio are chatty at DEBUG; keep them one notch quieter so our own
    # lines stay readable.
    for noisy in ("asyncio", "anyio", "mcp", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(root.level, logging.INFO))

    _configured = True
    logging.getLogger("agent_duet").info(
        "logging started: role=%s level=%s file=%s pid=%d",
        role,
        logging.getLevelName(root.level),
        path,
        os.getpid(),
    )
    return path


def reset_for_tests() -> None:
    """Allow a test process to reconfigure logging."""
    global _configured
    _configured = False
