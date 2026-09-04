#!/usr/bin/env python3
"""Detached worker spawning, PID-reuse-safe signalling, and the recursion guard.

A run must survive the MCP client that started it, so the worker is a separate process
in its own session. That makes killing it later dangerous: a bare PID can be reused.
Every signal here is therefore gated on the process's *start time* from
``/proc/<pid>/stat``, which a reused PID cannot match.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: Set in every child environment. Its presence in our own environment means we are a
#: child, and mutating tools must refuse.
CHILD_ENV_VAR = "AGENT_DUET_CHILD"

#: Environment variables a "minimal" child environment keeps. Chosen to preserve locale,
#: terminal behaviour, git/ssh, and each CLI's existing login without copying secrets.
MINIMAL_ENV_ALLOWLIST: tuple[str, ...] = (
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "SSH_AUTH_SOCK",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_CONFIG_GLOBAL",
    "GIT_EXEC_PATH",
    "GNUPGHOME",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


class RecursionError_(RuntimeError):
    """Raised when agent_duet is invoked from inside one of its own children."""


def is_child_process() -> bool:
    """Return whether this process was spawned by agent_duet as a child agent."""
    return os.environ.get(CHILD_ENV_VAR) == "1"


def refuse_if_child(operation: str) -> None:
    """Raise if the current process is an agent_duet child.

    This is the mechanical half of the "no child may invoke agent_duet" invariant; the
    other half is the empty strict MCP config handed to child Claude.
    """
    if is_child_process():
        raise RecursionError_(
            f"{operation} refused: {CHILD_ENV_VAR}=1 means this process is an "
            "agent_duet child agent. Children may never start, cancel, or finalize runs."
        )


def child_env(
    *, mode: str = "inherit", extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Build the environment for a child agent.

    ``inherit`` (the default) hands the child the operator's full environment so it can
    use every credential and tool the operator can. ``minimal`` restricts to
    :data:`MINIMAL_ENV_ALLOWLIST`. Either way :data:`CHILD_ENV_VAR` is forced on and
    values are never logged.
    """
    if mode == "minimal":
        env = {name: os.environ[name] for name in MINIMAL_ENV_ALLOWLIST if name in os.environ}
    else:
        env = dict(os.environ)
    env[CHILD_ENV_VAR] = "1"
    # A child must never inherit a pointer to our own MCP registration.
    env.pop("AGENT_DUET_CONFIG", None)
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Process identity
# ---------------------------------------------------------------------------


def process_start_ticks(pid: int) -> str | None:
    """Return field 22 of ``/proc/<pid>/stat`` (start time in clock ticks).

    Combined with the PID this is a stable process identity on Linux: a recycled PID
    gets a different start time, so we never signal the wrong process.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    # comm may contain spaces and parentheses; everything after the last ')' is safe.
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 2 :].split()
    # After comm, field indices shift by 2: starttime is field 22 overall => index 19.
    if len(fields) < 20:
        return None
    return fields[19]


def process_state(pid: int) -> str | None:
    """Return the single-character process state from ``/proc/<pid>/stat``.

    ``Z`` means the process has exited but has not been reaped. A detached worker is
    still a child of the MCP server process, so an exited worker sits as a zombie until
    the server reaps it; treating that as "alive" would hide a dead run forever.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 2 :].split()
    return fields[0] if fields else None


def reap_if_child(pid: int) -> bool:
    """Best-effort non-blocking reap of a direct child, so zombies do not pile up.

    The detached worker is still a child of the long-lived MCP server process. Returns
    True if this call reaped it.
    """
    if pid <= 0:
        return False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return False
    return reaped == pid


def process_alive(pid: int, start_ticks: str | None) -> bool:
    """Return whether ``pid`` is alive *and* is still the process we started."""
    if pid <= 0:
        return False
    if process_state(pid) == "Z":
        reap_if_child(pid)
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno in (errno.ESRCH, errno.EPERM):
            return exc.errno == errno.EPERM and start_ticks is None
        return False
    if start_ticks is None:
        return True
    return process_start_ticks(pid) == start_ticks


@dataclass(slots=True)
class SpawnedWorker:
    """Identity of a detached worker process."""

    pid: int
    pgid: int
    start_ticks: str | None
    stdout_log: Path
    stderr_log: Path


def spawn_detached_worker(
    argv: Sequence[str],
    *,
    cwd: Path,
    log_dir: Path,
    env: dict[str, str] | None = None,
) -> SpawnedWorker:
    """Start the worker in its own session with private, redirected log files."""
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stdout_log = log_dir / "worker.stdout.log"
    stderr_log = log_dir / "worker.stderr.log"
    out_fd = os.open(str(stdout_log), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    err_fd = os.open(str(stderr_log), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=out_fd,
            stderr=err_fd,
            start_new_session=True,
            env=env if env is not None else os.environ.copy(),
            close_fds=True,
        )
    finally:
        os.close(out_fd)
        os.close(err_fd)
    try:
        pgid = os.getpgid(process.pid)
    except OSError:  # pragma: no cover - the child exited immediately
        pgid = process.pid
    return SpawnedWorker(
        pid=process.pid,
        pgid=pgid,
        start_ticks=process_start_ticks(process.pid),
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )


def terminate_process_group(
    pgid: int,
    *,
    pid: int | None = None,
    start_ticks: str | None = None,
    grace_seconds: float = 10.0,
) -> str:
    """SIGTERM the group, wait a bounded grace period, then SIGKILL.

    Returns a short description of what happened. Never raises: cleanup failure must
    not stop the caller from recording state.
    """
    if pgid <= 1:
        return "refused: implausible process group"
    if pid is not None and start_ticks is not None and not process_alive(pid, start_ticks):
        return "already exited (or PID reused); no signal sent"

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "process group already gone"
    except PermissionError:
        return "permission denied signalling process group"

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return "terminated after SIGTERM"
        except PermissionError:
            return "permission denied checking process group"
        time.sleep(0.2)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated after SIGTERM (raced with SIGKILL)"
    except PermissionError:
        return "permission denied sending SIGKILL"

    # SIGKILL is asynchronous: wait for the kernel to actually tear the process down so
    # callers that immediately re-check liveness see the truth.
    if pid is not None:
        confirm = time.monotonic() + 5.0
        while time.monotonic() < confirm:
            if not process_alive(pid, start_ticks):
                break
            time.sleep(0.05)
        reap_if_child(pid)
    return "killed after grace period"


def self_worker_argv(run_id: str) -> list[str]:
    """Return the argv that re-executes this installation as a worker."""
    return [sys.executable, "-m", "agent_duet", "worker", "--run-id", run_id]
