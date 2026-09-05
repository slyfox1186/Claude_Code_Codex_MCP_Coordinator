#!/usr/bin/env python3
"""The stdio MCP server: five tools, every rule enforced here rather than by annotation.

Annotations are advisory metadata for clients. Nothing in this file trusts them. The
recursion guard, the repository allowlist, the lock, the state machine, and every
finalization precondition are re-checked server side on each call.

Stdout belongs to JSON-RPC. All diagnostics go to stderr through ``logging``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError
from mcp.types import ToolAnnotations

from . import __version__
from .artifacts import CRITIQUE_FILENAME, HANDOFF_FILENAME, scan_commit_set
from .config import (
    FOREGROUND_WAIT_MAX_SECONDS,
    LEGACY_WAIT_INPUT_MAX_SECONDS,
    Config,
    ConfigError,
    config_path,
    ensure_state_dirs,
    load_config,
)
from .git_guard import (
    GitError,
    RepoLockedError,
    combined_diff_sha256,
    commit,
    inspect_repo,
    lock_path_for,
    normalize_remote_url,
    owned_tree_sha,
    prune_worktrees,
    push_branch,
    remote_sha,
    remove_worktree,
    repo_lock,
    run_git,
    stage_paths,
    staged_paths,
    write_tree,
)
from .logging_setup import log_dir, setup_logging
from .models import (
    DeploymentEvidence,
    Evidence,
    FinalizeRequest,
    FinalizeResult,
    Phase,
    RunStatus,
    StartRequest,
    valid_branch,
)
from .process_guard import (
    RecursionError_,
    process_alive,
    refuse_if_child,
    self_worker_argv,
    spawn_detached_worker,
)
from .redact import redact
from .runners import cli_version
from .state import RunRecord, StateError, StateStore, default_next_action, new_run_dir, utcnow

logger = logging.getLogger("agent_duet")

INSTRUCTIONS = (
    "Use `agent_duet` for Claude->Codex->Claude work. Call `duet_start` once; retain "
    "`run_id`. Keep exactly one `duet_wait` in flight with "
    f"`timeout_seconds={FOREGROUND_WAIT_MAX_SECONDS}`; await it before new `duet_wait` "
    "or `duet_status`. If client backgrounds it, await it; do not repoll. Continue to "
    "terminal or `AWAITING_FINALIZE`. Never start a duplicate run or call from a child. "
    "At `AWAITING_FINALIZE`, summarize evidence and get user approval before "
    "`duet_finalize`. Never claim commit, push, deploy, or success without returned evidence."
)

mcp: MCPServer[Any] = MCPServer(
    "agent_duet",
    title="Agent Duet",
    instructions=INSTRUCTIONS,
    version=__version__,
)


class _Runtime:
    """Lazily loaded configuration and state store, shared by every tool call."""

    def __init__(self) -> None:
        self._config: Config | None = None
        self._store: StateStore | None = None
        self.config_path: Path | None = None

    def load(self) -> tuple[Config, StateStore]:
        if self._config is None or self._store is None:
            config = load_config(self.config_path)
            ensure_state_dirs(config)
            self._config = config
            self._store = StateStore(config.db_path)
        return self._config, self._store

    def reset(self) -> None:
        self._config = None
        self._store = None


RUNTIME = _Runtime()

# One stdio server can receive another tool request while Claude Code is tracking the
# first as a background task. Keep the original polling task alive if its request is
# cancelled, and refuse to create a second polling thread for the same durable run.
_ACTIVE_WAIT_TASKS: dict[str, asyncio.Task[RunRecord]] = {}


def _clear_active_wait(run_id: str, task: asyncio.Task[RunRecord]) -> None:
    if _ACTIVE_WAIT_TASKS.get(run_id) is task:
        _ACTIVE_WAIT_TASKS.pop(run_id, None)
    if not task.cancelled():
        # Retrieve a detached task's exception so client cancellation cannot produce an
        # unhandled-task warning. A still-attached caller receives the same exception.
        with contextlib.suppress(Exception):
            task.exception()


class ToolError(McpToolError):
    """An operator-facing error message. Raised instead of returning a fake status.

    It must subclass the SDK's ``ToolError``. That is the only exception type the tool
    layer treats as *anticipated*: it reaches the caller as ``is_error`` with this text
    intact. Anything else is treated as a crash, and the model is handed a bare
    "Error executing tool <name>" with the reason left behind in the server log --
    which makes every refusal here unactionable.
    """


def _fail(message: str) -> ToolError:
    return ToolError(redact(message)[:4000])


# ---------------------------------------------------------------------------
# duet_start
# ---------------------------------------------------------------------------


def create_run(config: Config, store: StateStore, request: StartRequest) -> RunRecord:
    """Validate the repository and durably create the run row.

    Separated from :func:`duet_start` so the record exists on disk before any process is
    spawned, and so tests can drive a run without a detached worker.
    """
    info = _validate_repo_for_start(config, request)

    reaped = _reap_dead_runs(store, str(info.path))
    if reaped:
        logger.info("duet_start: reaped %d crashed run(s) for %s", len(reaped), info.path)
    global_active = store.active_runs()
    if len(global_active) >= config.max_parallel_global:
        # Name them. Without this the caller has to go digging in sqlite to find out
        # what is holding the slot, which is exactly what the limit should tell them.
        blocking = "; ".join(
            f"{record.run_id} is {record.phase.value} on {record.repo_path}"
            for record in global_active
        )
        waiting = [r for r in global_active if r.phase is Phase.AWAITING_FINALIZE]
        advice = (
            f"call duet_finalize or duet_cancel on {waiting[0].run_id} -- it is waiting "
            "on you and will not advance by itself"
            if waiting
            else "wait for it to finish, or call duet_cancel on it"
        )
        raise _fail(
            f"cannot start: {len(global_active)} run(s) already active and "
            f"max_parallel_global is {config.max_parallel_global}. Active: {blocking}. "
            f"To proceed, {advice}."
        )
    if store.active_runs(str(info.path)):
        raise _fail(
            f"a run is already active for {info.path}; call duet_status on it instead of "
            "starting a duplicate"
        )
    try:
        with repo_lock(config.locks_dir, info.git_common_dir):
            pass  # Probe only: the worker takes the real lock for the run's lifetime.
    except RepoLockedError as exc:
        raise _fail(str(exc)) from exc

    run_id = str(uuid.uuid4())
    run_dir = new_run_dir(config.runs_dir, run_id)
    branch = (
        info.branch
        if request.delivery_mode == "direct_branch"
        else f"{config.git.branch_prefix}{run_id[:8]}"
    )
    if branch and not valid_branch(branch):
        raise _fail(f"computed branch name is unacceptable: {branch!r}")

    record = RunRecord(
        run_id=run_id,
        created_at=utcnow(),
        updated_at=utcnow(),
        phase=Phase.QUEUED,
        terminal=False,
        repo_path=str(info.path),
        git_common_dir=str(info.git_common_dir),
        delivery_mode=request.delivery_mode,
        task=request.task,
        run_dir=str(run_dir),
        acceptance_criteria=request.acceptance_criteria,
        branch=branch,
        base_sha=info.head_sha,
        current_sha=info.head_sha,
        idempotency_key=request.idempotency_key,
        summary="Queued. The worker is starting.",
        evidence={
            "start_remotes": info.remotes,
            "run_dir": str(run_dir),
            "host": socket.gethostname(),
            "claude_version": cli_version(config.claude_path),
            "codex_version": cli_version(config.codex_path),
        },
        host=socket.gethostname(),
        server_version=__version__,
        claude_version=cli_version(config.claude_path),
        codex_version=cli_version(config.codex_path),
    )
    try:
        store.create_run(record)
    except StateError as exc:
        raise _fail(str(exc)) from exc

    return record




@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )
)
async def duet_start(
    repo_path: str,
    task: str,
    acceptance_criteria: list[str] | None = None,
    delivery_mode: Literal["review_branch", "direct_branch"] | None = None,
    expected_base_ref: str | None = None,
    idempotency_key: str | None = None,
) -> RunStatus:
    """Start one Claude->Codex->Claude run and return immediately with its run_id.

    delivery_mode decides where the work ends up, so pick it deliberately:

    - ``direct_branch`` (the default) works on the branch the repository is already on,
      so finalize commits there -- to ``main`` if that is where the user is. This is what
      "make this change" normally means. It requires a clean tree and an attached HEAD,
      because the run edits the checkout in place.
    - ``review_branch`` parks the work on a new ``agent-duet/<id>`` branch that somebody
      then has to merge, and tolerates a dirty tree because it works in its own private
      worktree. Ask for it when the user wants to review before it touches their branch.

    Omit the argument to use the machine's ``git.default_delivery_mode``.

    Side effects: creates a durable run record, a branch and private worktree (in
    review_branch mode), and a detached worker process that will modify files in that
    worktree. It NEVER commits, pushes, deploys, changes remotes, or rewrites history;
    publishing is duet_finalize only. Call this once per task, keep the run_id, and poll
    with duet_wait.
    """
    refuse_if_child("duet_start")
    config, store = _runtime()
    # Unset means "whatever this machine is configured for". Resolving it here rather
    # than in the signature is what makes git.default_delivery_mode do anything at all:
    # a literal default in the signature can never be overridden by config, so the
    # setting sat in config.example.toml looking functional while being ignored.
    resolved_mode = delivery_mode or config.git.default_delivery_mode
    logger.info(
        "duet_start called: repo=%s delivery_mode=%s (caller=%s) criteria=%d task=%dB "
        "idempotency=%s",
        repo_path,
        resolved_mode,
        delivery_mode or "unset",
        len(acceptance_criteria or []),
        len(task),
        idempotency_key,
    )

    request = StartRequest(
        repo_path=Path(repo_path),
        task=task,
        acceptance_criteria=acceptance_criteria or [],
        delivery_mode=resolved_mode,
        expected_base_ref=expected_base_ref,
        idempotency_key=idempotency_key,
    )

    if request.idempotency_key:
        existing = store.find_by_idempotency_key(request.idempotency_key)
        if existing is not None:
            logger.info(
                "duet_start: idempotency key %r already maps to run %s (%s); returning it",
                request.idempotency_key,
                existing.run_id,
                existing.phase.value,
            )
            return existing.to_status()

    record = create_run(config, store, request)
    run_id = record.run_id
    run_dir = Path(record.run_dir)

    # The run row is durable before the worker exists, so a spawn failure is visible.
    try:
        worker = spawn_detached_worker(
            self_worker_argv(run_id),
            cwd=Path(__file__).resolve().parent.parent,
            log_dir=run_dir,
            env=_worker_env(config),
        )
    except OSError as exc:
        store.transition(
            run_id, Phase.FAILED, reason="worker spawn failed", error=str(exc)
        )
        raise _fail(f"could not spawn the worker process: {exc}") from exc

    logger.info(
        "duet_start: run %s queued on branch %s (worktree pending); worker pid=%d pgid=%d "
        "logs=%s",
        run_id,
        record.branch,
        worker.pid,
        worker.pgid,
        run_dir,
    )
    updated = store.update(
        run_id,
        worker_pid=worker.pid,
        worker_pgid=worker.pgid,
        worker_start_ticks=worker.start_ticks,
    )
    return updated.to_status(
        next_action=(
            f"Keep run_id {run_id}. Call duet_wait once with timeout_seconds="
            f"{FOREGROUND_WAIT_MAX_SECONDS}, then wait for that response before polling "
            "again. Continue until AWAITING_FINALIZE or terminal. Do not start another "
            "run for this task."
        )
    )


# ---------------------------------------------------------------------------
# duet_status / duet_wait
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def duet_status(run_id: UUID) -> RunStatus:
    """Return durable status and concise evidence for one run. No side effects."""
    _, store = _runtime()
    try:
        record = store.get(run_id)
    except StateError as exc:
        logger.warning("duet_status for unknown run %s", run_id)
        raise _fail(str(exc)) from exc
    status = _status_with_liveness(record)
    logger.info("duet_status %s -> %s (terminal=%s)", run_id, status.phase.value, status.terminal)
    return status


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def duet_wait(
    run_id: UUID, timeout_seconds: int = FOREGROUND_WAIT_MAX_SECONDS
) -> RunStatus:
    """Wait briefly for one run to change phase, then return its durable status.

    Accepts legacy inputs through 300 seconds, but the effective wait is always capped at
    90 seconds so Claude Code keeps the call in the foreground. Keep only one call in
    flight for a run and wait for its response before polling again. No side effects.
    """
    config, store = _runtime()
    if not 1 <= timeout_seconds <= LEGACY_WAIT_INPUT_MAX_SECONDS:
        raise _fail(
            "timeout_seconds must be between 1 and "
            f"{LEGACY_WAIT_INPUT_MAX_SECONDS}; got {timeout_seconds}"
        )
    bounded = min(
        int(timeout_seconds), config.wait_max_seconds, FOREGROUND_WAIT_MAX_SECONDS
    )
    try:
        current = store.get(run_id)
    except StateError as exc:
        raise _fail(str(exc)) from exc

    run_key = str(run_id)
    active = _ACTIVE_WAIT_TASKS.get(run_key)
    if active is not None and not active.done():
        logger.info("duet_wait %s: returning without starting a duplicate poller", run_id)
        status = _status_with_liveness(current)
        status.next_action = (
            "A duet_wait call is already active for this run. Do not start another poll "
            "or call duet_status; wait for the active call's result."
        )
        return status

    logger.info(
        "duet_wait %s: waiting up to %ds from phase %s", run_id, bounded, current.phase.value
    )
    wait_task = asyncio.create_task(
        asyncio.to_thread(
            store.wait_for_change,
            run_key,
            since=current.updated_at,
            timeout_seconds=bounded,
        )
    )
    _ACTIVE_WAIT_TASKS[run_key] = wait_task

    def clear_completed_wait(completed: asyncio.Task[RunRecord]) -> None:
        _clear_active_wait(run_key, completed)

    wait_task.add_done_callback(clear_completed_wait)
    record = await asyncio.shield(wait_task)
    status = _status_with_liveness(record)
    logger.info("duet_wait %s -> %s (terminal=%s)", run_id, status.phase.value, status.terminal)
    return status


# ---------------------------------------------------------------------------
# duet_cancel
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def duet_cancel(run_id: UUID) -> RunStatus:
    """Request cancellation and terminate the run's process group.

    Side effects: signals the detached worker and its children (SIGTERM, then SIGKILL
    after a grace period). Files the run already wrote are left in place; nothing is
    committed, pushed, or deployed. Safe to call more than once.
    """
    refuse_if_child("duet_cancel")
    logger.info("duet_cancel called for run %s", run_id)
    _, store = _runtime()
    try:
        record = store.get(run_id)
    except StateError as exc:
        raise _fail(str(exc)) from exc
    if record.terminal:
        return record.to_status(next_action="This run already finished; nothing to cancel.")

    store.request_cancel(run_id)
    outcomes = await asyncio.to_thread(_reap_run_processes, store, record)
    detail = "; ".join(outcomes) if outcomes else "cancel flag set"
    logger.info("duet_cancel %s: %s", run_id, detail)

    leftover = _surviving_processes(store.get(run_id))
    summary = "Run cancelled. Nothing was committed, pushed, or deployed."
    if leftover:
        summary += f" WARNING: {len(leftover)} process(es) could not be confirmed gone."
    updated = store.transition(
        run_id,
        Phase.CANCELLED,
        reason=f"cancelled by operator: {detail}",
        summary=summary,
        error=(
            f"cleanup incomplete: {', '.join(leftover)}" if leftover else None
        ),
    )
    return updated.to_status()


def cancel(run_id: str, config_path: Path | None = None) -> int:
    """Operator-facing ``agent-duet cancel``: clear a run without a client session.

    A run parked at ``AWAITING_FINALIZE`` has no live worker and is never reaped -- it
    is waiting for a person. It still counts as active, so with the default
    ``max_parallel_global = 1`` it blocks every new run, and until now the only way to
    release it was an interactive session that still had the MCP tools wired up.
    """
    try:
        config = load_config(config_path)
        ensure_state_dirs(config)
        store = StateStore(config.db_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    matches = [r for r in store.all_runs() if str(r.run_id).startswith(run_id)]
    if not matches:
        print(f"no run matches {run_id!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"{run_id!r} matches {len(matches)} runs; use more characters", file=sys.stderr)
        for record in matches:
            print(f"  {record.run_id}  {record.phase.value}", file=sys.stderr)
        return 1

    record = matches[0]
    if record.terminal:
        print(f"{record.run_id} is already {record.phase.value}; nothing to cancel")
        return 0

    store.request_cancel(record.run_id)
    outcomes = _reap_run_processes(store, record)
    leftover = _surviving_processes(store.get(record.run_id))
    summary = "Run cancelled. Nothing was committed, pushed, or deployed."
    if leftover:
        summary += f" WARNING: {len(leftover)} process(es) could not be confirmed gone."
    store.transition(
        record.run_id,
        Phase.CANCELLED,
        reason=f"cancelled from the command line: {'; '.join(outcomes) or 'no live process'}",
        summary=summary,
        error=f"cleanup incomplete: {', '.join(leftover)}" if leftover else None,
    )
    print(f"cancelled {record.run_id} (was {record.phase.value})")
    for line in outcomes:
        print(f"  {line}")
    if leftover:
        print(f"  WARNING: still running: {', '.join(leftover)}", file=sys.stderr)
        return 1
    return 0


def _reap_run_processes(store: StateStore, record: RunRecord) -> list[str]:
    """Terminate the active child agent first, then the worker itself.

    A child agent runs in its own session, so the worker's process group does not
    contain it. Signalling only the worker would kill the supervisor and leave a fully
    privileged Claude or Codex process running with nothing left to clean it up, so the
    child is reaped first and the worker second.
    """
    from .process_guard import terminate_process_group

    outcomes: list[str] = []
    if record.active_child_pgid:
        label = record.active_child_label or "child"
        outcomes.append(
            f"{label}: "
            + terminate_process_group(
                record.active_child_pgid,
                pid=record.active_child_pid,
                start_ticks=record.active_child_ticks,
            )
        )
        with contextlib.suppress(StateError):
            store.clear_active_child(record.run_id)
    if record.worker_pgid:
        outcomes.append(
            "worker: "
            + terminate_process_group(
                record.worker_pgid,
                pid=record.worker_pid,
                start_ticks=record.worker_start_ticks,
            )
        )
    return outcomes


def _surviving_processes(record: RunRecord) -> list[str]:
    """Return descriptions of run processes still alive after a reap attempt."""
    survivors: list[str] = []
    if record.active_child_pid and process_alive(
        record.active_child_pid, record.active_child_ticks
    ):
        survivors.append(f"{record.active_child_label or 'child'} pid {record.active_child_pid}")
    if record.worker_pid and process_alive(record.worker_pid, record.worker_start_ticks):
        survivors.append(f"worker pid {record.worker_pid}")
    return survivors


# ---------------------------------------------------------------------------
# duet_finalize
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )
)
async def duet_finalize(
    run_id: UUID,
    expected_branch: str,
    expected_remote_url: str,
    commit_message: str,
    expected_remote_name: str = "origin",
    push: bool = True,
    deployment_profile: str | None = None,
) -> FinalizeResult:
    """Publish a validated run: commit, optionally push, optionally verify deployment.

    Side effects: creates a real commit, pushes it to the named remote, and runs the
    configured deployment verifier. Only ever call this after the user has seen the
    evidence from duet_status/duet_wait and explicitly approved. Refuses unless the run
    is exactly AWAITING_FINALIZE and the branch, remote URL, and validated diff
    fingerprint all still match.
    """
    refuse_if_child("duet_finalize")
    logger.info(
        "duet_finalize called: run=%s branch=%s remote=%s push=%s profile=%s",
        run_id,
        expected_branch,
        expected_remote_name,
        push,
        deployment_profile,
    )
    request = FinalizeRequest(
        run_id=run_id,
        expected_branch=expected_branch,
        expected_remote_name=expected_remote_name,
        expected_remote_url=expected_remote_url,
        commit_message=commit_message,
        push=push,
        deployment_profile=deployment_profile,
    )
    config, store = _runtime()
    return await asyncio.to_thread(_finalize_blocking, config, store, request)


def _finalize_blocking(
    config: Config, store: StateStore, request: FinalizeRequest
) -> FinalizeResult:
    """The whole finalization sequence, under the repository lock."""
    run_key = str(request.run_id)
    try:
        record = store.get(run_key)
    except StateError as exc:
        raise _fail(str(exc)) from exc

    logger.info(
        "finalize %s: phase=%s branch=%s worktree=%s owned_paths=%d",
        run_key,
        record.phase.value,
        record.branch,
        record.worktree,
        len(record.owned_paths),
    )
    if record.phase is not Phase.AWAITING_FINALIZE:
        raise _fail(
            f"run {run_key} is {record.phase.value}, not AWAITING_FINALIZE; refusing to "
            "publish. Only a fully validated run may be finalized."
        )
    if record.branch != request.expected_branch:
        raise _fail(
            f"branch mismatch: the run is on {record.branch!r}, the request expects "
            f"{request.expected_branch!r}"
        )
    if request.expected_remote_name not in config.git.allowed_remote_names:
        raise _fail(
            f"remote {request.expected_remote_name!r} is not in allowed_remote_names "
            f"{config.git.allowed_remote_names}"
        )

    worktree = Path(record.worktree or record.repo_path)
    base = record.base_sha or ""

    try:
        with repo_lock(config.locks_dir, Path(record.git_common_dir)):
            return _finalize_locked(config, store, record, request, worktree, base)
    except RepoLockedError as exc:
        raise _fail(str(exc)) from exc
    except ToolError:
        # A precondition refusal. _finalize_locked either refused before entering
        # FINALIZING, or already recorded its own terminal state.
        _fail_if_stuck_finalizing(store, run_key, "finalization refused a precondition")
        raise
    except GitError as exc:
        store.transition(
            run_key, Phase.FAILED, reason="finalization git failure", error=str(exc)
        )
        raise _fail(f"finalization failed: {exc}") from exc
    except Exception as exc:
        # unexpected failure type, so every escape route ends in a durable terminal state.
        _fail_if_stuck_finalizing(
            store, run_key, f"unexpected finalization error: {type(exc).__name__}: {exc}"
        )
        raise _fail(f"finalization failed unexpectedly: {type(exc).__name__}: {exc}") from exc


def _fail_if_stuck_finalizing(store: StateStore, run_key: str, reason: str) -> None:
    """Record a durable FAILED state if a run is still sitting in FINALIZING.

    Entering FINALIZING means a commit may already exist. That commit is preserved: the
    run is marked failed with the precise partial state and nothing is reset or retried.
    """
    with contextlib.suppress(StateError):
        current = store.get(run_key)
        if current.phase is not Phase.FINALIZING:
            return
        logger.error("run %s stuck in FINALIZING: %s", run_key, reason)
        store.transition(
            current.run_id,
            Phase.FAILED,
            reason="finalization did not complete",
            error=redact(reason)[:2000],
            summary=(
                "Finalization stopped partway. Any local commit already created is "
                "preserved; nothing was reset or retried automatically."
            ),
        )


def _finalize_locked(
    config: Config,
    store: StateStore,
    record: RunRecord,
    request: FinalizeRequest,
    worktree: Path,
    base: str,
) -> FinalizeResult:
    run_key = record.run_id
    info = inspect_repo(worktree)

    if info.branch != request.expected_branch:
        raise _fail(
            f"the worktree is on branch {info.branch!r}, not {request.expected_branch!r}"
        )
    if info.head_sha != base:
        raise _fail(
            f"HEAD is {info.head_sha[:12]} but the validated base was {base[:12]}; the "
            "repository moved after validation. Re-run instead of publishing stale work."
        )

    actual_remote = info.remotes.get(request.expected_remote_name)
    if actual_remote is None:
        raise _fail(
            f"remote {request.expected_remote_name!r} does not exist in {worktree}; "
            f"available remotes: {sorted(info.remotes)}"
        )
    if normalize_remote_url(actual_remote) != normalize_remote_url(request.expected_remote_url):
        raise _fail(
            f"remote URL mismatch for {request.expected_remote_name!r}: the repository "
            f"has {normalize_remote_url(actual_remote)!r}, the request expects "
            f"{normalize_remote_url(request.expected_remote_url)!r}"
        )
    # The caller's expectation alone is not enough: a write-capable phase could have
    # rewritten the remote, and a finalize request that repeats the new value would
    # otherwise bless it. The remote recorded at run creation is immutable evidence.
    recorded_remotes = record.evidence.get("start_remotes")
    if isinstance(recorded_remotes, dict):
        recorded_url = recorded_remotes.get(request.expected_remote_name)
        if recorded_url is None:
            raise _fail(
                f"remote {request.expected_remote_name!r} did not exist when this run "
                "started; refusing to push to a remote the run never validated"
            )
        if normalize_remote_url(recorded_url) != normalize_remote_url(actual_remote):
            raise _fail(
                f"remote {request.expected_remote_name!r} changed during the run: it was "
                f"{normalize_remote_url(recorded_url)!r} at start and is now "
                f"{normalize_remote_url(actual_remote)!r}. Refusing to publish."
            )

    current_diff = combined_diff_sha256(worktree, base, record.owned_paths)
    logger.info(
        "finalize %s: diff fingerprint validated=%s current=%s",
        run_key,
        record.validated_diff_sha256,
        current_diff,
    )
    if current_diff != record.validated_diff_sha256:
        raise _fail(
            "the working tree changed after validation "
            f"(validated {str(record.validated_diff_sha256)[:12]}, now {current_diff[:12]}); "
            "refusing to publish unvalidated work"
        )

    owned = list(record.owned_paths)
    if not owned:
        raise _fail("this run changed no files; there is nothing to commit")
    for name in (HANDOFF_FILENAME, CRITIQUE_FILENAME):
        if name in owned:
            raise _fail(f"{name} is a coordination artifact and must not be committed")
        if (worktree / name).exists():
            raise _fail(f"{name} is still present in the worktree; refusing to commit")

    # Content-exact gate. The textual diff above cannot see a symlink retargeted between
    # two identical files, or a mode change; the tree id can.
    current_tree = owned_tree_sha(
        worktree, base, owned, Path(record.run_dir) / "finalize.index"
    )
    if record.validated_tree_sha and current_tree != record.validated_tree_sha:
        raise _fail(
            f"the content to be committed changed after validation (validated tree "
            f"{record.validated_tree_sha[:12]}, now {current_tree[:12]}); refusing to "
            "publish unvalidated work"
        )

    report = scan_commit_set(worktree, owned)
    if not report.safe:
        raise _fail("refusing to commit: " + "; ".join(report.refusals))
    if report.warnings:
        logger.warning("finalize %s: commit-set warnings: %s", run_key, report.warnings)
        store.merge_evidence(run_key, {"commit_safety_warnings": report.warnings})

    store.transition(
        run_key,
        Phase.FINALIZING,
        reason="all finalization preconditions verified",
        summary="Committing the validated change.",
    )

    stage_paths(worktree, owned)
    staged = staged_paths(worktree)
    unexpected = sorted(set(staged) - set(owned))
    if unexpected:
        run_git(["reset"], cwd=worktree, check=False)
        store.transition(
            run_key,
            Phase.FAILED,
            reason="unexpected staged paths",
            error=f"unexpected staged paths: {unexpected}",
        )
        raise _fail(f"refusing to commit unexpected staged paths: {unexpected}")

    tree = write_tree(worktree)
    logger.info("finalize %s: staged %d path(s), tree=%s", run_key, len(staged), tree)
    if record.validated_tree_sha and tree != record.validated_tree_sha:
        run_git(["reset"], cwd=worktree, check=False)
        store.transition(
            run_key,
            Phase.FAILED,
            reason="staged tree does not match the validated tree",
            error=f"validated tree {record.validated_tree_sha}, staged tree {tree}",
        )
        raise _fail(
            f"the staged tree {tree[:12]} does not match the validated tree "
            f"{record.validated_tree_sha[:12]}; refusing to commit"
        )
    local_sha = commit(worktree, request.commit_message)
    logger.info("finalize %s: created commit %s", run_key, local_sha)

    result = FinalizeResult(
        run_id=run_key,
        phase=Phase.FINALIZING,
        terminal=False,
        branch=request.expected_branch,
        local_commit_sha=local_sha,
        staged_paths=staged,
        tree_sha=tree,
        validation_manifest=record.evidence.get("validation_manifest"),
        remote_name=request.expected_remote_name,
        remote_url=normalize_remote_url(actual_remote),
    )

    if not request.push:
        store.transition(
            run_key,
            Phase.COMPLETE,
            reason="committed locally; push not requested",
            summary=f"Committed {local_sha[:12]} locally. Not pushed, not deployed.",
            current_sha=local_sha,
        )
        result.phase = Phase.COMPLETE
        result.terminal = True
        result.summary = f"Committed {local_sha} locally. Push was not requested."
        result.next_action = "Report the exact local commit SHA. Nothing was pushed."
        return result

    try:
        push_branch(worktree, request.expected_remote_name, request.expected_branch)
    except GitError as exc:
        store.transition(
            run_key,
            Phase.FAILED,
            reason="push failed after a successful local commit",
            error=f"local commit {local_sha} exists but push failed: {exc}",
            current_sha=local_sha,
        )
        raise _fail(
            f"the local commit {local_sha} was created but the push failed: {exc}. "
            "The commit is preserved; nothing was reset or retried automatically."
        ) from exc

    observed = remote_sha(worktree, request.expected_remote_name, request.expected_branch)
    logger.info(
        "finalize %s: pushed to %s/%s; remote ref resolves to %s",
        run_key,
        request.expected_remote_name,
        request.expected_branch,
        observed,
    )
    result.pushed = True
    result.remote_commit_sha = observed
    if observed != local_sha:
        store.transition(
            run_key,
            Phase.FAILED,
            reason="remote ref does not match the local commit",
            error=f"local {local_sha} != remote {observed}",
            current_sha=local_sha,
        )
        raise _fail(
            f"push reported success but the remote ref is {observed}, not {local_sha}. "
            "Investigate before claiming the change is published."
        )

    deployment = _verify_deployment(config, record, request, local_sha, worktree)
    logger.info(
        "finalize %s: deployment=%s (%s)", run_key, deployment.status, deployment.detail
    )
    result.deployment = deployment

    if deployment.status == "FAILED":
        store.transition(
            run_key,
            Phase.FAILED,
            reason="deployment verification failed after a successful push",
            error=f"deployment verifier: {deployment.detail}",
            current_sha=local_sha,
        )
        result.phase = Phase.FAILED
        result.terminal = True
        result.summary = (
            f"Commit {local_sha} was pushed and the remote ref matches, but deployment "
            f"verification failed: {deployment.detail}"
        )
        result.next_action = (
            "Report the pushed SHA and the deployment failure. Do not retry blindly."
        )
        return result

    store.transition(
        run_key,
        Phase.COMPLETE,
        reason="committed, pushed, and remote ref verified",
        summary=(
            f"Committed {local_sha[:12]} and pushed to {request.expected_remote_name}/"
            f"{request.expected_branch}; remote ref verified. Deployment: {deployment.status}."
        ),
        current_sha=local_sha,
        evidence={"deployment": deployment.model_dump()},
    )
    result.phase = Phase.COMPLETE
    result.terminal = True
    result.summary = (
        f"Commit {local_sha} pushed to {request.expected_remote_name}/"
        f"{request.expected_branch}; the remote ref resolves to the same SHA. "
        f"Deployment status: {deployment.status}."
    )
    result.next_action = (
        "Report the exact local and remote SHAs and the deployment status verbatim. "
        "If deployment is NOT_CHECKED, say so; do not describe it as successful."
    )
    return result


def _verify_deployment(
    config: Config,
    record: RunRecord,
    request: FinalizeRequest,
    commit_sha: str,
    worktree: Path,
) -> DeploymentEvidence:
    """Run the configured verifier, or report NOT_CHECKED. Never assumes success."""
    profile_name = request.deployment_profile
    if profile_name is None:
        repo_cfg = config.repository_for(Path(record.repo_path))
        profile_name = repo_cfg.deployment_profile if repo_cfg else None
    if not profile_name:
        return DeploymentEvidence(
            status="NOT_CHECKED", detail="no deployment profile configured for this repository"
        )
    if not config.deployment.enabled:
        return DeploymentEvidence(
            status="NOT_CHECKED", detail="deployment verification is disabled in config"
        )
    profile = config.deployment.profiles.get(profile_name)
    if profile is None:
        return DeploymentEvidence(
            status="FAILED", detail=f"unknown deployment profile {profile_name!r}"
        )
    if profile.expected_remote_url and normalize_remote_url(
        profile.expected_remote_url
    ) != normalize_remote_url(request.expected_remote_url):
        return DeploymentEvidence(
            status="FAILED",
            detail=(
                f"profile {profile_name!r} is bound to a different remote than the one "
                "just pushed"
            ),
        )

    try:
        completed = subprocess.run(
            profile.command,
            cwd=str(worktree),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=profile.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DeploymentEvidence(
            status="FAILED", detail=f"verifier could not run: {type(exc).__name__}: {exc}"
        )
    if completed.returncode != 0:
        return DeploymentEvidence(
            status="FAILED",
            detail=f"verifier exited {completed.returncode}: {redact(completed.stderr)[:500]}",
        )
    try:
        payload = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return DeploymentEvidence(
            status="FAILED",
            detail="verifier output was not JSON; a verifier must emit deployed_sha, "
            "health, checked_at, and target",
        )
    missing = [
        key for key in ("deployed_sha", "health", "checked_at", "target") if key not in payload
    ]
    if missing:
        return DeploymentEvidence(
            status="FAILED", detail=f"verifier output is missing {missing}"
        )
    deployed = str(payload["deployed_sha"])
    health = str(payload["health"])
    if deployed != commit_sha:
        return DeploymentEvidence(
            status="FAILED",
            deployed_sha=deployed,
            health=health,
            checked_at=str(payload["checked_at"]),
            target=str(payload["target"]),
            detail=f"deployed SHA {deployed} does not match the pushed commit {commit_sha}",
        )
    if health.lower() not in {"healthy", "ok", "passing", "green"}:
        return DeploymentEvidence(
            status="FAILED",
            deployed_sha=deployed,
            health=health,
            checked_at=str(payload["checked_at"]),
            target=str(payload["target"]),
            detail=f"verifier reported health={health!r}",
        )
    return DeploymentEvidence(
        status="VERIFIED",
        deployed_sha=deployed,
        health=health,
        checked_at=str(payload["checked_at"]),
        target=str(payload["target"]),
        detail="verifier confirmed the exact pushed SHA is deployed and healthy",
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _runtime() -> tuple[Config, StateStore]:
    try:
        return RUNTIME.load()
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


def _worker_env(config: Config) -> dict[str, str]:
    """Environment for the detached worker.

    The worker is the coordinator, not a child agent, so it must NOT carry
    ``AGENT_DUET_CHILD``; it needs the same config the server used.
    """
    env = dict(os.environ)
    env.pop("AGENT_DUET_CHILD", None)
    if config.source_path is not None:
        env["AGENT_DUET_CONFIG"] = str(config.source_path)
    return env


def _validate_repo_for_start(config: Config, request: StartRequest) -> Any:
    """Apply the full repository guard and return the inspected repository."""
    try:
        info = inspect_repo(request.repo_path)
    except (GitError, FileNotFoundError, OSError) as exc:
        raise _fail(f"repository check failed: {exc}") from exc

    if not config.root_allows(info.path):
        raise _fail(
            f"{info.path} is not below an allowed_repo_roots entry "
            f"({config.allowed_repo_roots}); add it to config.toml if it should be"
        )
    if info.submodules:
        logger.warning(
            "repository %s has submodules; they are not managed by agent_duet", info.path
        )
    if request.expected_base_ref:
        resolved = run_git(
            ["rev-parse", "--verify", f"{request.expected_base_ref}^{{commit}}"],
            cwd=info.path,
            check=False,
        )
        if not resolved.ok:
            raise _fail(f"expected_base_ref {request.expected_base_ref!r} does not resolve")
        if resolved.stdout.strip() != info.head_sha:
            raise _fail(
                f"expected_base_ref resolves to {resolved.stdout.strip()[:12]} but HEAD is "
                f"{info.head_sha[:12]}"
            )
    if request.delivery_mode == "direct_branch":
        if not info.clean:
            raise _fail(
                "refusing an in-place run on a dirty working tree; commit or stash your "
                "changes, or use delivery_mode=review_branch"
            )
        if info.detached:
            raise _fail("refusing direct_branch on a detached HEAD")
    elif not info.clean:
        logger.info(
            "repository %s is dirty; review_branch mode will branch from HEAD and leave "
            "those changes untouched",
            info.path,
        )
    return info


def _worker_vanished(record: RunRecord) -> bool:
    """Return whether a non-terminal run's worker process is gone."""
    if record.terminal or record.phase is Phase.AWAITING_FINALIZE:
        return False
    if not record.worker_pid:
        return False
    return not process_alive(record.worker_pid, record.worker_start_ticks)


def _status_with_liveness(record: RunRecord) -> RunStatus:
    """Return the status, reporting a vanished worker WITHOUT writing to the database.

    ``duet_status`` and ``duet_wait`` are annotated read-only, so they must not mutate
    anything. A dead worker is therefore reported here and persisted by the next
    mutating call (:func:`_reap_dead_runs` from ``duet_start``, or ``duet_cancel``).
    """
    if not _worker_vanished(record):
        return record.to_status(next_action=default_next_action(record.phase))
    status = record.to_status()
    status.phase = Phase.FAILED
    status.terminal = True
    status.error = (
        f"the worker process (pid {record.worker_pid}) exited without reaching a "
        f"terminal state while the run was {record.phase.value}; see the run directory "
        "logs. Nothing was committed, pushed, or deployed."
    )
    status.summary = "Worker died unexpectedly. Nothing was committed, pushed, or deployed."
    status.next_action = (
        "Report the failure and the preserved evidence. This run cannot be resumed; "
        "start a new one if the work is still wanted."
    )
    return status


def _reap_dead_runs(store: StateStore, repo_path: str) -> list[str]:
    """Persist FAILED for runs on ``repo_path`` whose worker vanished.

    Called from the mutating ``duet_start`` path so a crashed run cannot block the
    repository forever while the read-only status tools stay side-effect free.
    """
    reaped: list[str] = []
    for record in store.active_runs(repo_path):
        if not _worker_vanished(record):
            continue
        logger.error(
            "run %s is %s but its worker (pid %s) is gone; marking it failed",
            record.run_id,
            record.phase.value,
            record.worker_pid,
        )
        with contextlib.suppress(StateError):
            store.transition(
                record.run_id,
                Phase.FAILED,
                reason="worker process is no longer running",
                error=(
                    f"the worker process (pid {record.worker_pid}) exited without "
                    "reaching a terminal state; see the run directory logs"
                ),
                summary="Worker died unexpectedly. Nothing was committed, pushed, or deployed.",
            )
            reaped.append(record.run_id)
    return reaped


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def doctor(config_path: Path | None = None) -> int:
    """Print a health report to stdout. Never prints credentials. CLI only."""
    lines: list[str] = [f"agent-duet {__version__}", f"python {sys.version.split()[0]}"]
    problems = 0
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print("\n".join(lines))
        print(f"CONFIG: FAIL - {exc}")
        return 1
    setup_logging("doctor", state_dir=config.state_path, level=config.log_level, to_stderr=False)
    logger.info("doctor running")
    lines.append(f"config: {config.source_path}")
    lines.append(f"logs:   {log_dir(config.state_path)}")
    try:
        mode = oct(Path(str(config.source_path)).stat().st_mode & 0o777)
        lines.append(f"config permissions: {mode}{'' if mode == '0o600' else '  (expected 0o600)'}")
        if mode != "0o600":
            problems += 1
    except OSError:
        pass

    lines.append(f"claude: {config.claude_path} -> {cli_version(config.claude_path)}")
    lines.append(f"codex:  {config.codex_path} -> {cli_version(config.codex_path)}")
    lines.append(f"git:    {cli_version(Path('git'))}")
    claude_posture = (
        "dangerously-skip-permissions"
        if config.claude.dangerously_skip_permissions
        else config.claude.permission_mode
    )
    lines.append(
        f"child access: claude={claude_posture}, codex sandbox={config.codex.sandbox_mode}, "
        f"env={config.child_env_mode}"
    )
    lines.append(
        f"child quality: claude effort={config.claude.effort}, "
        f"codex reasoning={config.codex.reasoning_effort}, "
        f"phase safety ceiling={config.phase_timeout_seconds}s"
    )
    lines.append(
        "status poll: "
        f"{min(config.wait_max_seconds, FOREGROUND_WAIT_MAX_SECONDS)}s effective "
        f"({config.wait_max_seconds}s configured)"
    )

    try:
        ensure_state_dirs(config)
        state_mode = oct(config.state_path.stat().st_mode & 0o777)
        lines.append(f"state dir: {config.state_path} ({state_mode})")
        if state_mode != "0o700":
            problems += 1
            lines.append("  WARNING: expected 0o700")
        store = StateStore(config.db_path)
        active = store.active_runs()
        lines.append(f"sqlite: OK, {len(store.all_runs())} runs, {len(active)} active")
    except Exception as exc:
        problems += 1
        lines.append(f"state: FAIL - {exc}")

    lines.append(f"allowed repo roots: {config.allowed_repo_roots}")
    for repo in config.repositories:
        exists = Path(repo.path).is_dir()
        lines.append(
            f"  repo {repo.path}: {'present' if exists else 'MISSING'}, "
            f"{len(repo.validation_commands)} validation command(s)"
        )
        if not exists:
            problems += 1
        if not repo.validation_commands:
            # Not a failure -- a repo may genuinely have no suite -- but it must be
            # stated. With no command, FINAL_VALIDATING has nothing to run, so the
            # only thing standing behind the work is what the models claim about it.
            lines.append(
                "    WARNING: no validation_commands, so nothing independent checks "
                "this repository's runs. Both agents' claims go unverified."
            )
    lines.append(
        f"deployment: {'enabled' if config.deployment.enabled else 'disabled'}, "
        f"{len(config.deployment.profiles)} profile(s)"
    )
    lines.append(f"checked at: {datetime.now(UTC).isoformat(timespec='seconds')}")
    lines.append(f"result: {'OK' if problems == 0 else f'{problems} problem(s)'}")
    print("\n".join(lines))
    return 0 if problems == 0 else 1


def gc(older_than_days: int, *, apply: bool = False, config_path: Path | None = None) -> int:
    """List, and optionally remove, everything a terminal run older than N days left behind.

    That is three separate leaks, not one. The artifact directories are the obvious one.
    The second is git's own worktree registration: deleting a worktree directory without
    telling git leaves a stale entry in the real repository forever. The third is the
    database row, which used to outlive its artifacts and keep showing up in
    ``agent-duet runs`` pointing at a repository that may no longer exist.

    Branches are the deliberate exception. A run's branch holds its work, so this reports
    the ones it is orphaning and leaves deleting them to a person.
    """
    from datetime import timedelta

    config = load_config(config_path)
    store = StateStore(config.db_path)
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)

    expired: list[RunRecord] = []
    for record in store.all_runs():
        if not record.terminal:
            continue
        try:
            updated = datetime.fromisoformat(record.updated_at)
        except ValueError:
            continue
        if updated < cutoff:
            expired.append(record)

    if not expired:
        print("gc: nothing older than the cutoff")
        return 0

    def _removable(raw: str | None, root: Path) -> Path | None:
        """A path is only removable if it still exists *and* sits under our own root."""
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_dir() and root in path.parents else None

    print(f"gc: {len(expired)} run(s) older than {older_than_days} day(s):")
    for record in expired:
        paths = [
            p
            for p in (
                _removable(record.run_dir, config.runs_dir),
                _removable(record.worktree, config.worktrees_dir),
            )
            if p is not None
        ]
        detail = ", ".join(str(p) for p in paths) or "no artifacts left on disk"
        print(f"  {record.run_id}  {record.phase.value}  {detail}")
        if record.branch and Path(record.repo_path).is_dir():
            print(
                f"      leaves branch {record.branch} in {record.repo_path} "
                "(not deleted; it holds the run's work)"
            )
    if not apply:
        print("gc: dry run. Re-run with --apply to remove exactly these.")
        return 0

    import shutil

    pruned: set[Path] = set()
    for record in expired:
        run_dir = _removable(record.run_dir, config.runs_dir)
        if run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
            print(f"gc: removed {run_dir}")
        worktree = _removable(record.worktree, config.worktrees_dir)
        if worktree is not None:
            repo = Path(record.repo_path)
            # Ask git first so the registration goes with the directory. Falling back to
            # rmtree + prune covers a worktree git can no longer reach -- a moved or
            # deleted origin repository, which is exactly when this rots unnoticed.
            removed = (
                remove_worktree(repo, worktree, force=True)
                if repo.is_dir()
                else None
            )
            if removed is None or not removed.ok:
                shutil.rmtree(worktree, ignore_errors=True)
            print(f"gc: removed {worktree}")
            if repo.is_dir() and repo not in pruned:
                prune_worktrees(repo)
                pruned.add(repo)

    forgotten = store.delete_runs([record.run_id for record in expired])
    print(f"gc: forgot {forgotten} run(s) from the database")
    return 0


def serve() -> None:
    """Run the stdio MCP server.

    Logging goes to a file under the state directory *and* to stderr; stdout carries
    nothing but JSON-RPC. The log file matters because MCP clients routinely discard a
    stdio server's stderr, and a config failure at startup is exactly when the detail is
    needed.
    """
    if RUNTIME.config_path is None:
        override = os.environ.get("AGENT_DUET_CONFIG")
        if override:
            RUNTIME.config_path = Path(override)

    state_dir: Path | None = None
    level: str | None = None
    config_error: Exception | None = None
    try:
        probe = load_config(RUNTIME.config_path)
        state_dir = probe.state_path
        level = probe.log_level
    except ConfigError as exc:
        config_error = exc

    log_file = setup_logging("server", state_dir=state_dir, level=level)
    logger.info(
        "agent_duet %s starting: pid=%d python=%s config=%s log=%s",
        __version__,
        os.getpid(),
        sys.version.split()[0],
        RUNTIME.config_path or config_path(),
        log_file,
    )
    if config_error is not None:
        logger.error(
            "configuration could not be loaded; every tool call will fail until this is "
            "fixed: %s",
            config_error,
        )
    else:
        logger.info("configuration loaded; state dir %s", state_dir)

    try:
        refuse_if_child("starting the agent_duet MCP server")
    except RecursionError_ as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    logger.info("serving %d tools over stdio", 5)
    try:
        mcp.run(transport="stdio")
    except Exception:
        logger.exception("the stdio server exited with an unhandled exception")
        raise
    finally:
        logger.info("stdio server stopped")


__all__ = [
    "Evidence",
    "RunStatus",
    "create_run",
    "doctor",
    "duet_cancel",
    "duet_finalize",
    "duet_start",
    "duet_status",
    "duet_wait",
    "gc",
    "lock_path_for",
    "mcp",
    "serve",
]
