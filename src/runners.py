#!/usr/bin/env python3
"""Adapters that invoke the two CLIs as child processes.

Both adapters share the same shape: build an argv *list* (never a shell string), feed
the whole dynamic prompt on stdin so task text never lands in a process listing, stream
both output streams into bounded private log files, and return a structured result.

Flag choices are pinned to the CLIs actually installed on this machine and verified
against ``--help``; where ``BUILD_SPEC.md`` names a flag that no longer exists, the
deviation is documented inline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import BoundedLog, atomic_write_text
from .config import ClaudeConfig, CodexConfig, Config
from .process_guard import child_env, process_start_ticks
from .redact import redact

logger = logging.getLogger("agent_duet.runners")

#: Claude Code needs a positional prompt even when the real instructions arrive on
#: stdin; this sentinel points it at stdin without carrying any task content.
STDIN_POINTER = "Follow the complete instructions supplied on stdin."


class RunnerError(RuntimeError):
    """Raised when a child CLI could not be started or produced unusable output."""


@dataclass(slots=True)
class ChildResult:
    """The measured outcome of one child CLI invocation."""

    argv: list[str]
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    duration_seconds: float
    stdout_log: Path
    stderr_log: Path
    stdout_text: str = ""
    final_message: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


def _empty_mcp_config(run_dir: Path) -> Path:
    """Write the empty strict MCP config handed to every child Claude."""
    path = run_dir / "empty-mcp.json"
    atomic_write_text(path, json.dumps({"mcpServers": {}}) + "\n", mode=0o600)
    return path


def build_claude_argv(cfg: ClaudeConfig, executable: Path, run_dir: Path) -> list[str]:
    """Build the argv for one non-interactive Claude Code phase.

    Deviation from ``BUILD_SPEC.md``: Claude Code 2.1.x has no ``--max-turns`` and no
    ``--permission-prompts`` flag. Turn limiting is expressed through the optional
    ``max_budget_usd`` cap instead, and prompt suppression through the permission mode.
    """
    argv: list[str] = [
        str(executable),
        "-p",
        STDIN_POINTER,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        str(_empty_mcp_config(run_dir)),
    ]

    # Recursion guard: the child can never reach agent_duet's own tools.
    disallowed = list(cfg.disallowed_tools)
    if "mcp__*" not in disallowed:
        disallowed.append("mcp__*")
    argv += ["--disallowedTools", *disallowed]

    if cfg.dangerously_skip_permissions:
        # Full machine access by operator decision; see SECURITY.md.
        argv.append("--dangerously-skip-permissions")
    else:
        argv += ["--permission-mode", cfg.permission_mode]

    if cfg.allowed_tools:
        argv += ["--allowedTools", *cfg.allowed_tools]
    for directory in cfg.extra_dirs:
        argv += ["--add-dir", directory]
    if cfg.model:
        argv += ["--model", cfg.model]
    if cfg.max_budget_usd > 0:
        argv += ["--max-budget-usd", str(cfg.max_budget_usd)]
    argv += list(cfg.extra_args)
    return argv


def build_codex_argv(
    cfg: CodexConfig, executable: Path, worktree: Path, last_message_path: Path
) -> list[str]:
    """Build the argv for the non-interactive Codex review phase.

    Deviation from ``BUILD_SPEC.md``: ``codex exec`` in 0.153.x has no
    ``--ask-for-approval``; non-interactive runs never prompt, and the sandbox posture
    is selected with ``--sandbox`` or the explicit bypass flag.
    """
    argv: list[str] = [str(executable), "exec"]

    if cfg.sandbox_mode == "bypass":
        # Full machine access by operator decision; see SECURITY.md.
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv += ["--sandbox", cfg.sandbox_mode]

    if cfg.ephemeral:
        argv.append("--ephemeral")
    if cfg.ignore_user_config:
        argv.append("--ignore-user-config")
    if cfg.model:
        argv += ["--model", cfg.model]

    if not cfg.ignore_user_config:
        # Only meaningful when a user config is actually loaded: the override merges
        # into an existing [mcp_servers.agent_duet] table to switch it off. Passing it
        # alongside --ignore-user-config would instead CREATE a partial table with no
        # transport, and codex rejects that with "invalid transport".
        argv += ["-c", "mcp_servers.agent_duet.enabled=false"]
    argv += [
        "--json",
        "-C",
        str(worktree),
        "--output-last-message",
        str(last_message_path),
    ]
    argv += list(cfg.extra_args)
    argv.append("-")  # stdin carries the entire prompt
    return argv


async def _pump(
    stream: asyncio.StreamReader | None,
    log: BoundedLog,
    *,
    collect: list[bytes] | None = None,
    collect_limit: int = 8_000_000,
) -> None:
    """Copy a child stream into a bounded log, optionally collecting it in memory."""
    if stream is None:
        return
    collected = 0
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            return
        log.append(chunk)
        if collect is not None and collected < collect_limit:
            collect.append(chunk)
            collected += len(chunk)


async def run_child(
    argv: list[str],
    *,
    prompt: str,
    cwd: Path,
    log_dir: Path,
    log_prefix: str,
    timeout_seconds: int,
    max_log_bytes: int,
    env_mode: str = "inherit",
    cancel_check: Any = None,
    on_spawn: Any = None,
    on_exit: Any = None,
) -> ChildResult:
    """Run one child CLI to completion under a bounded timeout.

    ``cancel_check`` is an optional zero-argument callable polled while waiting; when it
    returns True the whole process group is terminated and ``cancelled`` is set.

    ``on_spawn(pid, pgid, start_ticks)`` is called as soon as the child exists, and
    ``on_exit()`` once it is reaped. The child runs in its *own* session, so its process
    group is not the worker's; recording it durably is what lets ``duet_cancel`` reap a
    running agent instead of orphaning it.
    """
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stdout_log = BoundedLog.create(log_dir / f"{log_prefix}.stdout.log", max_log_bytes)
    stderr_log = BoundedLog.create(log_dir / f"{log_prefix}.stderr.log", max_log_bytes)
    # The exact argv is evidence; the prompt is not stored here because it lives in the
    # run directory as its own artifact.
    atomic_write_text(log_dir / f"{log_prefix}.argv.json", json.dumps(argv, indent=2) + "\n")

    loop = asyncio.get_running_loop()
    started = loop.time()
    logger.info(
        "starting child %s: cwd=%s timeout=%ds env_mode=%s prompt=%dB",
        log_prefix,
        cwd,
        timeout_seconds,
        env_mode,
        len(prompt),
    )
    logger.debug("child %s argv: %s", log_prefix, argv)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=child_env(mode=env_mode),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RunnerError(f"could not execute {argv[0]}: {exc}") from exc

    try:
        child_pgid = os.getpgid(process.pid)
    except OSError:  # pragma: no cover - the child exited before we could look
        child_pgid = process.pid
    if on_spawn is not None:
        on_spawn(process.pid, child_pgid, process_start_ticks(process.pid))

    stdout_chunks: list[bytes] = []
    pumps = asyncio.gather(
        _pump(process.stdout, stdout_log, collect=stdout_chunks),
        _pump(process.stderr, stderr_log),
    )

    async def _feed_stdin() -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                process.stdin.close()

    feeder = asyncio.create_task(_feed_stdin())

    timed_out = False
    cancelled = False
    try:
        if cancel_check is None:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        else:
            deadline = loop.time() + timeout_seconds
            while True:
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                    break
                except TimeoutError:
                    if cancel_check():
                        cancelled = True
                        break
                    if loop.time() >= deadline:
                        timed_out = True
                        break
    except TimeoutError:
        timed_out = True

    if timed_out or cancelled:
        _terminate(process)
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(process.wait(), timeout=10)
        if process.returncode is None:
            _terminate(process, hard=True)
            with contextlib.suppress(Exception):
                await process.wait()

    with contextlib.suppress(Exception):
        await feeder
    with contextlib.suppress(Exception):
        await pumps

    if on_exit is not None:
        on_exit()

    duration = loop.time() - started
    logger.info(
        "child %s finished: exit=%s timed_out=%s cancelled=%s duration=%.1fs stdout=%dB "
        "logs=%s",
        log_prefix,
        process.returncode,
        timed_out,
        cancelled,
        duration,
        len(b"".join(stdout_chunks)),
        stdout_log.path.parent,
    )
    return ChildResult(
        argv=argv,
        exit_code=process.returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_seconds=round(duration, 3),
        stdout_log=stdout_log.path,
        stderr_log=stderr_log.path,
        stdout_text=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
    )


def _terminate(process: asyncio.subprocess.Process, *, hard: bool = False) -> None:
    """Signal the child's whole process group, tolerating a race with its exit."""
    sig = signal.SIGKILL if hard else signal.SIGTERM
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), sig)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def parse_claude_output(text: str) -> tuple[dict[str, Any], str]:
    """Parse ``claude --output-format json`` stdout into (payload, final message).

    Claude emits a single JSON object; older builds emitted an array of messages. Both
    are handled, and anything else is an error rather than a guess.
    """
    stripped = text.strip()
    if not stripped:
        raise RunnerError("claude produced no stdout; expected a JSON result object")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # Tolerate leading noise by retrying from the first '{' or '['.
        for opener in ("{", "["):
            index = stripped.find(opener)
            if index > 0:
                with contextlib.suppress(json.JSONDecodeError):
                    payload = json.loads(stripped[index:])
                    break
        else:
            raise RunnerError(f"claude stdout was not valid JSON: {exc}") from exc

    if isinstance(payload, list):
        payload = payload[-1] if payload else {}
    if not isinstance(payload, dict):
        raise RunnerError("claude stdout JSON was not an object")

    if payload.get("is_error"):
        detail = str(payload.get("result") or payload.get("error") or "unknown error")
        raise RunnerError(f"claude reported an error result: {redact(detail)[:500]}")

    message = payload.get("result")
    if not isinstance(message, str):
        message = ""
    return payload, message


def parse_codex_events(text: str) -> tuple[list[dict[str, Any]], str, bool]:
    """Parse ``codex exec --json`` JSONL into (events, final message, completed).

    The event vocabulary has changed across Codex releases, so completion is detected
    from any event whose type or msg-type ends in ``turn.completed`` /
    ``task_complete``, and the final message is taken from the last agent-message-ish
    payload. The authoritative final text is the ``--output-last-message`` file; this
    parse is the cross-check.
    """
    events: list[dict[str, Any]] = []
    final_message = ""
    completed = False
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)

        kind = str(event.get("type") or "")
        msg = event.get("msg")
        if isinstance(msg, dict):
            kind = str(msg.get("type") or kind)
        lowered = kind.lower()

        if lowered.endswith(("turn.completed", "task_complete", "turn_complete")):
            completed = True
        if lowered.endswith(("turn.failed", "error", "task_failed")):
            completed = completed or False

        candidate = _extract_message(event)
        if candidate:
            final_message = candidate
    return events, final_message, completed


def _extract_message(event: dict[str, Any]) -> str:
    """Pull agent message text out of one Codex event, whatever shape it uses."""
    for container in (event, event.get("msg"), event.get("item")):
        if not isinstance(container, dict):
            continue
        kind = str(container.get("type") or "").lower()
        if "agent_message" in kind or "assistant" in kind or kind.endswith("message"):
            for key in ("message", "text", "content", "last_agent_message"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value
                if isinstance(value, list):
                    parts = [
                        part.get("text", "")
                        for part in value
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    ]
                    joined = "".join(parts).strip()
                    if joined:
                        return joined
    value = event.get("last_agent_message")
    return value if isinstance(value, str) else ""


def cli_version(executable: Path, arg: str = "--version") -> str:
    """Return a CLI's version string, or a short failure note. Never raises."""
    try:
        completed = subprocess.run(
            [str(executable), arg],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable ({type(exc).__name__})"
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return output[0][:200] if output else "unknown"


def resolve_runtime_versions(config: Config) -> dict[str, str]:
    """Return version strings for both child CLIs and git."""
    return {
        "claude": cli_version(config.claude_path),
        "codex": cli_version(config.codex_path),
        "git": cli_version(Path("git")),
    }
