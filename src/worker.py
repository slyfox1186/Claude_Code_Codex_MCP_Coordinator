#!/usr/bin/env python3
"""The durable worker: one process, one run, the whole Claude -> Codex -> Claude cycle.

The worker is spawned detached by ``duet_start`` and owns the repository lock for the
lifetime of the run. It writes every transition to SQLite before doing the work that
follows it, so a crash leaves an accurate record rather than a plausible-looking lie.

Nothing in here commits, pushes, or deploys. Publishing is ``duet_finalize`` only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .artifacts import (
    CRITIQUE_FILENAME,
    HANDOFF_FILENAME,
    ArtifactError,
    archive_and_remove,
    atomic_write_text,
    read_text_bounded,
    tail_text,
    validate_critique,
    validate_handoff,
    write_critique,
    write_json,
)
from .config import Config, ensure_state_dirs, load_config
from .git_guard import (
    GitError,
    RepoLockedError,
    add_worktree,
    changed_paths,
    combined_diff_sha256,
    fingerprint,
    inspect_repo,
    owned_tree_sha,
    repo_lock,
    run_git,
)
from .logging_setup import setup_logging
from .models import Phase, ValidationResult
from .process_guard import child_env
from .redact import redact, scan_for_secrets
from .runners import (
    RunnerError,
    build_claude_argv,
    build_codex_argv,
    parse_claude_output,
    parse_codex_events,
    run_child,
)
from .state import RunRecord, StateStore, utcnow

logger = logging.getLogger("agent_duet.worker")

PROMPTS_DIR = Path(__file__).parent / "prompts"


class PhaseFailure(RuntimeError):
    """A phase failed its invariants. Carries the operator-facing reason."""


class Cancelled(RuntimeError):
    """The run was cancelled cooperatively between or during phases."""


TEMPLATE_NAMES = ("claude_implement.md", "codex_review.md", "claude_reconcile.md")


def _load_template(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _pin_templates(run_dir: Path) -> Path:
    """Copy the prompt templates into the run directory and return that directory.

    A worker outlives the session that started it -- often by an hour -- and
    ``_load_template`` reads from the installed package at the moment each phase begins.
    Upgrading agent-duet while a run is in flight therefore hands a running worker a
    template its own code was never written for, and the run dies on a missing
    placeholder *after* the expensive phases have already succeeded. Observed exactly
    that: phase 1 and the Codex review both finished, then reconciliation raised
    ``KeyError: 'timeout_minutes'``.

    Pinning at the start makes each run use one consistent set of templates, and leaves
    the exact prompts used sitting in the run directory as evidence.
    """
    pinned = run_dir / "prompts"
    pinned.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in TEMPLATE_NAMES:
        target = pinned / name
        if not target.exists():  # Idempotent: never re-pin a run already under way.
            atomic_write_text(target, _load_template(name))
    return pinned


def _format_criteria(criteria: list[str]) -> str:
    if not criteria:
        return "(none supplied; use your judgement and state your assumptions)"
    return "\n".join(f"- {item}" for item in criteria)


@dataclass(slots=True)
class Worker:
    """Executes one run to ``AWAITING_FINALIZE`` or a terminal failure."""

    config: Config
    store: StateStore
    run_id: str

    # -- helpers -----------------------------------------------------------

    @property
    def record(self) -> RunRecord:
        return self.store.get(self.run_id)

    def _run_dir(self) -> Path:
        path = Path(self.record.run_dir)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def _phase_timeout_seconds(self, provider_timeout_seconds: int) -> int:
        """Return the actual phase ceiling enforced by ``run_child``."""
        return min(provider_timeout_seconds, self.config.phase_timeout_seconds)

    def _phase_timeout_description(self, provider_timeout_seconds: int) -> str:
        seconds = self._phase_timeout_seconds(provider_timeout_seconds)
        minutes, remainder = divmod(seconds, 60)
        if remainder == 0:
            return f"{minutes} minute{'s' if minutes != 1 else ''} ({seconds} seconds)"
        return f"{seconds} seconds"

    def _template(self, name: str) -> str:
        """Read one prompt template from this run's pinned copy, not the package."""
        return (_pin_templates(self._run_dir()) / name).read_text(encoding="utf-8")

    def _check_cancel(self) -> None:
        if self.store.get(self.run_id).cancel_requested:
            raise Cancelled("cancellation requested by the operator")

    def _track_child(self, label: str) -> tuple[object, object]:
        """Return (on_spawn, on_exit) hooks that record the active child durably.

        A child agent runs in its own session and therefore its own process group.
        Recording that group is what lets ``duet_cancel`` terminate a running Claude or
        Codex process instead of killing only the worker and orphaning the agent.
        """

        def on_spawn(pid: int, pgid: int, ticks: str | None) -> None:
            try:
                self.store.set_active_child(
                    self.run_id, pid=pid, pgid=pgid, ticks=ticks, label=label
                )
            except Exception:  # pragma: no cover - bookkeeping must not kill the run
                logger.warning("could not record the active child for %s", self.run_id)

        def on_exit() -> None:
            try:
                self.store.clear_active_child(self.run_id)
            except Exception:  # pragma: no cover
                logger.warning("could not clear the active child for %s", self.run_id)

        return on_spawn, on_exit

    def _cancel_requested(self) -> bool:
        try:
            return self.store.get(self.run_id).cancel_requested
        except Exception:  # a transient DB error must not kill the run
            logger.warning("could not read the cancel flag for run %s", self.run_id)
            return False

    # -- entry point -------------------------------------------------------

    async def execute(self) -> None:
        """Run every phase, translating failures into durable terminal states."""
        record = self.record
        repo = Path(record.repo_path)
        try:
            with repo_lock(self.config.locks_dir, Path(record.git_common_dir)):
                await self._execute_locked(repo)
        except Cancelled as exc:
            logger.warning("run %s cancelled: %s", self.run_id, exc)
            self._terminal(Phase.CANCELLED, str(exc))
        except RepoLockedError as exc:
            self._terminal(Phase.FAILED, str(exc))
        except PhaseFailure as exc:
            self._terminal(Phase.FAILED, str(exc))
        except (GitError, ArtifactError, RunnerError) as exc:
            self._terminal(Phase.FAILED, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            self._terminal(Phase.FAILED, f"unexpected worker error: {type(exc).__name__}: {exc}")

    def _terminal(self, phase: Phase, reason: str) -> None:
        safe = redact(str(reason))[:4000]
        try:
            self.store.transition(
                self.run_id,
                phase,
                reason=safe,
                error=safe if phase is Phase.FAILED else None,
                summary=(
                    "Run cancelled before finalization; nothing was committed or pushed."
                    if phase is Phase.CANCELLED
                    else "Run failed. Evidence preserved in the run directory."
                ),
            )
        except Exception:  # already terminal, or the DB is unavailable
            logger.exception("could not record the terminal state of run %s", self.run_id)

    async def _execute_locked(self, repo: Path) -> None:
        logger.info("run %s: repository lock acquired", self.run_id)
        _pin_templates(self._run_dir())
        worktree = self._prepare_worktree(repo)
        logger.info("run %s: working tree is %s", self.run_id, worktree)
        self._check_cancel()

        await self._phase_implement(worktree)
        self._check_cancel()

        handoff_digest = self._phase_validate_handoff(worktree)
        self._check_cancel()

        critique_text, integrity_note = await self._phase_review(worktree)
        self._check_cancel()

        critique_digest = self._phase_write_critique(worktree, critique_text)
        self._check_cancel()

        proposed_message = await self._phase_reconcile(worktree, integrity_note)
        self._check_cancel()

        self._phase_final_validation(
            worktree,
            handoff_digest=handoff_digest,
            critique_digest=critique_digest,
            proposed_message=proposed_message,
        )

    # -- worktree ----------------------------------------------------------

    def _prepare_worktree(self, repo: Path) -> Path:
        """Revalidate the durable start record, then create the tree this run owns.

        ``duet_start`` validated the repository and recorded ``base_sha`` *before*
        releasing its probe lock, so HEAD can move before this worker takes the real
        lock. The recorded baseline is authoritative: it is re-checked here and used to
        create the worktree, never replaced by a fresh observation. Otherwise a run
        could silently rebase itself and defeat ``expected_base_ref``.
        """
        record = self.record
        info = inspect_repo(repo)

        if str(info.git_common_dir) != record.git_common_dir:
            raise PhaseFailure(
                f"the repository moved between start and worker: git common dir was "
                f"{record.git_common_dir}, now {info.git_common_dir}"
            )

        recorded_remotes = record.evidence.get("start_remotes")
        if isinstance(recorded_remotes, dict) and info.remotes != recorded_remotes:
            raise PhaseFailure(
                f"remotes changed between start and worker: {sorted(recorded_remotes)} "
                f"-> {sorted(info.remotes)}"
            )

        base = record.base_sha
        if not base:
            raise PhaseFailure("the run record has no recorded base commit")
        if info.head_sha != base:
            raise PhaseFailure(
                f"HEAD moved from the validated base {base[:12]} to "
                f"{info.head_sha[:12]} before this run could start. Refusing to "
                "silently change the baseline; start a new run."
            )

        if record.delivery_mode == "direct_branch":
            if not info.clean:
                raise PhaseFailure(
                    "refusing an in-place run: the working tree is dirty. Commit or "
                    "stash your changes, or use delivery_mode=review_branch."
                )
            if info.detached:
                raise PhaseFailure("refusing direct_branch on a detached HEAD")
            self.store.update(
                self.run_id,
                worktree=str(repo),
                branch=info.branch,
                current_sha=base,
            )
            return repo

        worktree = Path(record.worktree) if record.worktree else None
        if worktree and worktree.is_dir() and (worktree / ".git").exists():
            return worktree  # Resumed after a crash: reuse the existing worktree.

        branch = record.branch or f"{self.config.git.branch_prefix}{self.run_id[:8]}"
        repo_id = repo.name or "repo"
        target = self.config.worktrees_dir / f"{repo_id}-{self.run_id[:8]}" / self.run_id[:8]
        add_worktree(repo, target, branch, base)
        self.store.update(
            self.run_id,
            worktree=str(target),
            branch=branch,
            current_sha=base,
        )
        return target

    # -- phase 1 -----------------------------------------------------------

    async def _phase_implement(self, worktree: Path) -> None:
        record = self.store.transition(
            self.run_id,
            Phase.CLAUDE_IMPLEMENTING,
            reason="starting the implementation phase",
            summary="Claude is implementing the task.",
        )
        info = inspect_repo(worktree)
        phase_timeout = self._phase_timeout_seconds(self.config.claude.timeout_seconds)
        prompt = self._template("claude_implement.md").format(
            worktree=worktree,
            timeout_description=self._phase_timeout_description(
                self.config.claude.timeout_seconds
            ),
            repo_path=record.repo_path,
            branch=record.branch or info.branch or "(detached)",
            base_sha=record.base_sha or info.head_sha,
            upstream=info.upstream or "(none)",
            delivery_mode=record.delivery_mode,
            starting_status="clean" if info.clean else "not clean",
            task=record.task,
            acceptance_criteria=_format_criteria(record.acceptance_criteria),
            handoff_filename=HANDOFF_FILENAME,
            critique_filename=CRITIQUE_FILENAME,
        )
        run_dir = self._run_dir()
        atomic_write_text(run_dir / "phase1.prompt.md", prompt)
        argv = build_claude_argv(self.config.claude, self.config.claude_path, run_dir)
        _spawn_0, _exit_0 = self._track_child("phase1-claude")
        result = await run_child(
            argv,
            prompt=prompt,
            cwd=worktree,
            log_dir=run_dir,
            log_prefix="phase1-claude",
            timeout_seconds=phase_timeout,
            max_log_bytes=self.config.log_max_bytes_per_stream,
            env_mode=self.config.child_env_mode,
            cancel_check=self._cancel_requested,
            on_spawn=_spawn_0,
            on_exit=_exit_0,
        )
        if result.cancelled:
            raise Cancelled("cancelled during the implementation phase")
        if result.timed_out:
            raise PhaseFailure(
                "the implementation phase exceeded its "
                f"{phase_timeout}s timeout"
            )
        if not result.ok:
            raise PhaseFailure(
                f"claude exited {result.exit_code} during implementation. "
                f"stderr tail: {tail_text(result.stderr_log, 800)}"
            )
        payload, message = parse_claude_output(result.stdout_text)
        write_json(run_dir / "phase1.claude.json", payload)
        atomic_write_text(run_dir / "phase1.final_message.md", redact(message))

    # -- handoff gate ------------------------------------------------------

    def _phase_validate_handoff(self, worktree: Path) -> str:
        record = self.store.transition(
            self.run_id,
            Phase.HANDOFF_VALIDATING,
            reason="checking the implementation handoff and repository invariants",
            summary="Validating the implementation handoff.",
        )
        info = inspect_repo(worktree)
        base = record.base_sha or ""
        if info.head_sha != base:
            raise PhaseFailure(
                f"the implementation phase moved HEAD from {base[:12]} to "
                f"{info.head_sha[:12]}; committing is the coordinator's job, not the agent's"
            )
        expected_remotes = json.loads(
            json.dumps(record.evidence.get("start_remotes", info.remotes))
        )
        if info.remotes != expected_remotes:
            raise PhaseFailure(
                f"remotes changed during implementation: {sorted(expected_remotes)} -> "
                f"{sorted(info.remotes)}"
            )
        if (worktree / CRITIQUE_FILENAME).exists():
            raise PhaseFailure(
                f"the implementation phase created {CRITIQUE_FILENAME}; only the "
                "coordinator may write the reviewer's report"
            )
        _, digest = validate_handoff(worktree)
        logger.info(
            "run %s: handoff accepted (sha256=%s); HEAD and remotes unchanged",
            self.run_id,
            digest,
        )
        self.store.merge_evidence(self.run_id, {"handoff_sha256": digest})
        return digest

    # -- phase 2 -----------------------------------------------------------

    async def _phase_review(self, worktree: Path) -> tuple[str, str]:
        record = self.store.transition(
            self.run_id,
            Phase.CODEX_REVIEWING,
            reason="starting the independent review phase",
            summary="Codex is reviewing the change.",
        )
        before = fingerprint(worktree)
        run_dir = self._run_dir()
        write_json(
            run_dir / "phase2.fingerprint.before.json",
            {
                "head_sha": before.head_sha,
                "porcelain_sha256": before.porcelain_sha256,
                "working_diff_sha256": before.working_diff_sha256,
                "staged_diff_sha256": before.staged_diff_sha256,
                "remotes": before.remotes,
                "branch": before.branch,
            },
        )

        phase_timeout = self._phase_timeout_seconds(self.config.codex.timeout_seconds)
        prompt = self._template("codex_review.md").format(
            worktree=worktree,
            timeout_description=self._phase_timeout_description(
                self.config.codex.timeout_seconds
            ),
            base_sha=record.base_sha or "",
            current_sha=before.head_sha,
            branch=record.branch or "(detached)",
            handoff_filename=HANDOFF_FILENAME,
            critique_filename=CRITIQUE_FILENAME,
            task=record.task,
        )
        atomic_write_text(run_dir / "phase2.prompt.md", prompt)

        last_message_path = run_dir / "phase2.codex_last_message.md"
        argv = build_codex_argv(
            self.config.codex, self.config.codex_path, worktree, last_message_path
        )
        _spawn_1, _exit_1 = self._track_child("phase2-codex")
        result = await run_child(
            argv,
            prompt=prompt,
            cwd=worktree,
            log_dir=run_dir,
            log_prefix="phase2-codex",
            timeout_seconds=phase_timeout,
            max_log_bytes=self.config.log_max_bytes_per_stream,
            env_mode=self.config.child_env_mode,
            cancel_check=self._cancel_requested,
            on_spawn=_spawn_1,
            on_exit=_exit_1,
        )
        if result.cancelled:
            raise Cancelled("cancelled during the review phase")
        if result.timed_out:
            raise PhaseFailure(
                f"the review phase exceeded its {phase_timeout}s timeout"
            )
        if not result.ok:
            raise PhaseFailure(
                f"codex exited {result.exit_code} during review. "
                f"stderr tail: {tail_text(result.stderr_log, 800)}"
            )

        events, parsed_message, completed = parse_codex_events(result.stdout_text)
        if not completed:
            raise PhaseFailure(
                "the review phase produced no turn-completed event; treating the "
                "review as unusable rather than guessing"
            )
        # The --output-last-message file is authoritative; the JSONL parse is a fallback.
        critique_text = ""
        if last_message_path.is_file():
            critique_text = read_text_bounded(last_message_path).strip()
        if not critique_text:
            critique_text = parsed_message.strip()
        if not critique_text:
            raise PhaseFailure("the reviewer returned no final message")

        after = fingerprint(worktree)
        write_json(
            run_dir / "phase2.fingerprint.after.json",
            {
                "head_sha": after.head_sha,
                "porcelain_sha256": after.porcelain_sha256,
                "working_diff_sha256": after.working_diff_sha256,
                "staged_diff_sha256": after.staged_diff_sha256,
                "remotes": after.remotes,
                "branch": after.branch,
            },
        )
        mutations = before.differences(after)
        logger.info(
            "run %s: review returned %d JSONL events and a %dB report; repository "
            "mutations detected: %s",
            self.run_id,
            len(events),
            len(critique_text),
            mutations or "none",
        )
        integrity_note = self._apply_review_integrity_policy(mutations, len(events))
        return critique_text, integrity_note

    def _apply_review_integrity_policy(self, mutations: list[str], event_count: int) -> str:
        """Record whether the reviewer mutated the tree and apply the configured policy."""
        self.store.transition(
            self.run_id,
            Phase.REVIEW_INTEGRITY_CHECK,
            reason=f"comparing pre/post review fingerprints ({event_count} events parsed)",
            summary="Checking that the review did not change the repository.",
            evidence={
                "codex_readonly_verified": not mutations,
                "codex_mutations_detected": mutations,
            },
        )
        if not mutations:
            return ""
        detail = "; ".join(mutations)
        if self.config.codex.write_policy == "fail":
            raise PhaseFailure(
                f"the reviewer mutated the repository ({detail}); write_policy=fail"
            )
        return (
            "- NOTE: the reviewer ran with write access and the working tree changed "
            f"during its turn ({detail}). Treat those changes as unreviewed and verify "
            "them yourself before keeping them."
        )

    def _phase_write_critique(self, worktree: Path, critique_text: str) -> str:
        """The single trusted write of the reviewer's report into the worktree."""
        validate_critique(critique_text)
        findings = scan_for_secrets(critique_text)
        text = critique_text
        if findings:
            text = redact(critique_text)
        logger.info(
            "run %s: coordinator writing %s (%dB, redacted=%s)",
            self.run_id,
            CRITIQUE_FILENAME,
            len(text),
            bool(findings),
        )
        # One canonical byte contract: exactly one trailing newline, so the archived
        # copy, the worktree copy, and the recorded digest always agree.
        text = text.rstrip("\n") + "\n"
        _, digest = write_critique(worktree, text)
        run_dir = self._run_dir()
        atomic_write_text(run_dir / "phase2.critique.md", text)
        self.store.merge_evidence(
            self.run_id,
            {
                "critique_sha256": digest,
                "critique_redacted": bool(findings),
            },
        )
        return digest

    # -- phase 3 -----------------------------------------------------------

    async def _phase_reconcile(self, worktree: Path, integrity_note: str) -> str:
        record = self.store.transition(
            self.run_id,
            Phase.CLAUDE_RECONCILING,
            reason="starting the reconciliation phase",
            summary="Claude is adjudicating the critique and fixing justified findings.",
        )
        info = inspect_repo(worktree)
        phase_timeout = self._phase_timeout_seconds(self.config.claude.timeout_seconds)
        prompt = self._template("claude_reconcile.md").format(
            worktree=worktree,
            timeout_description=self._phase_timeout_description(
                self.config.claude.timeout_seconds
            ),
            repo_path=record.repo_path,
            branch=record.branch or info.branch or "(detached)",
            base_sha=record.base_sha or "",
            current_sha=info.head_sha,
            review_integrity_note=integrity_note,
            task=record.task,
            acceptance_criteria=_format_criteria(record.acceptance_criteria),
            handoff_filename=HANDOFF_FILENAME,
            critique_filename=CRITIQUE_FILENAME,
        )
        run_dir = self._run_dir()
        atomic_write_text(run_dir / "phase3.prompt.md", prompt)
        argv = build_claude_argv(self.config.claude, self.config.claude_path, run_dir)
        _spawn_2, _exit_2 = self._track_child("phase3-claude")
        result = await run_child(
            argv,
            prompt=prompt,
            cwd=worktree,
            log_dir=run_dir,
            log_prefix="phase3-claude",
            timeout_seconds=phase_timeout,
            max_log_bytes=self.config.log_max_bytes_per_stream,
            env_mode=self.config.child_env_mode,
            cancel_check=self._cancel_requested,
            on_spawn=_spawn_2,
            on_exit=_exit_2,
        )
        if result.cancelled:
            raise Cancelled("cancelled during the reconciliation phase")
        if result.timed_out:
            raise PhaseFailure(
                "the reconciliation phase exceeded its "
                f"{phase_timeout}s timeout"
            )
        if not result.ok:
            raise PhaseFailure(
                f"claude exited {result.exit_code} during reconciliation. "
                f"stderr tail: {tail_text(result.stderr_log, 800)}"
            )
        payload, message = parse_claude_output(result.stdout_text)
        write_json(run_dir / "phase3.claude.json", payload)
        atomic_write_text(run_dir / "phase3.final_message.md", redact(message))
        return _extract_commit_message(message)

    # -- final validation --------------------------------------------------

    def _phase_final_validation(
        self,
        worktree: Path,
        *,
        handoff_digest: str,
        critique_digest: str,
        proposed_message: str,
    ) -> None:
        record = self.store.transition(
            self.run_id,
            Phase.FINAL_VALIDATING,
            reason="running the coordinator's own validation commands",
            summary="Running configured validation commands.",
        )
        run_dir = self._run_dir()

        # Coordination artifacts leave the worktree before validation so the tree that
        # gets validated is exactly the tree that gets committed.
        archived = archive_and_remove(
            worktree, run_dir, (HANDOFF_FILENAME, CRITIQUE_FILENAME)
        )
        logger.info("run %s: archived and removed %s from the worktree", self.run_id, archived)

        info = inspect_repo(worktree)
        base = record.base_sha or ""
        if info.head_sha != base:
            raise PhaseFailure(
                f"HEAD moved to {info.head_sha[:12]} during reconciliation; the "
                "coordinator owns committing"
            )
        # The reconciliation phase is write-capable, so the remote set is re-checked
        # here as well. A rewritten remote would otherwise reach finalization, where the
        # caller-supplied URL could unknowingly bless it.
        recorded_remotes = record.evidence.get("start_remotes")
        if isinstance(recorded_remotes, dict) and info.remotes != recorded_remotes:
            raise PhaseFailure(
                f"remotes changed during reconciliation: {sorted(recorded_remotes)} -> "
                f"{sorted(info.remotes)}"
            )

        # Snapshot what the AGENTS changed, before any validation command runs. A test
        # suite legitimately writes __pycache__, .coverage, build output and the like;
        # those are the validator's artifacts, not the run's work, and sweeping them
        # into the commit set would publish them. The set is fixed here and everything
        # downstream uses it.
        owned = changed_paths(worktree, base)
        logger.debug("run %s: run-owned paths before validation: %s", self.run_id, owned)

        repo_cfg = self.config.repository_for(Path(record.repo_path))
        results: list[ValidationResult] = []
        if repo_cfg and repo_cfg.validation_commands:
            for index, vector in enumerate(repo_cfg.validation_commands):
                self._check_cancel()
                results.append(
                    self._run_validation(
                        vector,
                        worktree,
                        run_dir / f"validation-{index}.log",
                        repo_cfg.validation_timeout_seconds,
                    )
                )
                if not results[-1].passed:
                    manifest = self._write_manifest(run_dir, results)
                    raise PhaseFailure(
                        f"validation command {index} ({' '.join(vector)}) failed with "
                        f"exit code {results[-1].exit_code}. Manifest: {manifest}"
                    )

        manifest = self._write_manifest(run_dir, results)
        produced = sorted(set(changed_paths(worktree, base)) - set(owned))
        if produced:
            logger.info(
                "run %s: ignoring %d path(s) produced by validation, not by the agents: %s",
                self.run_id,
                len(produced),
                produced[:20],
            )
        diff_sha = combined_diff_sha256(worktree, base, owned)
        # The authoritative gate: the exact git tree committing these paths would build.
        tree_sha = owned_tree_sha(worktree, base, owned, run_dir / "validation.index")
        logger.info(
            "run %s: %d validation(s) passed; %d changed path(s); diff fingerprint %s",
            self.run_id,
            len(results),
            len(owned),
            diff_sha,
        )
        logger.debug("run %s: changed paths: %s", self.run_id, owned)

        # An empty result set means no command was configured, not that checks passed.
        # Saying "every configured validation passed" there is true but reads as a
        # clean bill of health, so state the absence outright.
        unvalidated = not results
        summary = (
            "Reconciliation complete, but this repository has NO configured "
            "validation_commands, so nothing independent verified the work -- only "
            "the agents' own claims stand behind it. Nothing has been committed, "
            "pushed, or deployed."
            if unvalidated
            else (
                f"Reconciliation complete and all {len(results)} configured "
                "validation(s) passed. Nothing has been committed, pushed, or deployed."
            )
        )

        self.store.transition(
            self.run_id,
            Phase.AWAITING_FINALIZE,
            reason=(
                "all phases passed; NO validation commands are configured"
                if unvalidated
                else "all phases and configured validations passed"
            ),
            summary=summary,
            current_sha=info.head_sha,
            validated_diff_sha256=diff_sha,
            validated_tree_sha=tree_sha,
            owned_paths=owned,
            evidence={
                "validation_manifest": str(manifest),
                "critique_archived": CRITIQUE_FILENAME in archived,
                "handoff_sha256": handoff_digest,
                "critique_sha256": critique_digest,
                "working_diff_sha256": diff_sha,
                "validated_tree_sha": tree_sha,
                "proposed_commit_message": proposed_message,
                "validations": [item.model_dump() for item in results],
                "unvalidated": unvalidated,
                "changed_path_count": len(owned),
                "validation_produced_paths": produced,
                "run_dir": str(run_dir),
            },
        )

    def _run_validation(
        self, vector: list[str], worktree: Path, log_path: Path, timeout: int
    ) -> ValidationResult:
        """Run one trusted command vector and record exactly what happened."""
        started = utcnow()
        start_clock = time.monotonic()
        exit_code: int | None
        with log_path.open("wb") as handle:
            os.chmod(log_path, 0o600)
            try:
                import subprocess  # local import keeps the module import list honest

                completed = subprocess.run(
                    vector,
                    cwd=str(worktree),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=child_env(mode=self.config.child_env_mode),
                    check=False,
                )
                exit_code = completed.returncode
                handle.write(redact(completed.stdout).encode())
                handle.write(b"\n--- stderr ---\n")
                handle.write(redact(completed.stderr).encode())
            except subprocess.TimeoutExpired:
                exit_code = None
                handle.write(f"[timed out after {timeout}s]\n".encode())
            except (OSError, ValueError) as exc:
                exit_code = None
                handle.write(f"[could not execute: {type(exc).__name__}: {exc}]\n".encode())
        del start_clock
        logger.info(
            "run %s: validation %s -> exit=%s", self.run_id, " ".join(vector), exit_code
        )
        return ValidationResult(
            argv=list(vector),
            cwd=str(worktree),
            exit_code=exit_code,
            started_at=started,
            ended_at=utcnow(),
            passed=exit_code == 0,
            log_path=str(log_path),
            tail=tail_text(log_path, 1500),
        )

    def _write_manifest(self, run_dir: Path, results: list[ValidationResult]) -> Path:
        path = run_dir / "validation-manifest.json"
        write_json(
            path,
            {
                "run_id": self.run_id,
                "written_at": utcnow(),
                "server_version": __version__,
                "host": socket.gethostname(),
                "results": [item.model_dump() for item in results],
            },
        )
        return path


def _extract_commit_message(message: str) -> str:
    """Pull the proposed commit message out of the reconciliation response."""
    for line in message.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("COMMIT_MESSAGE:"):
            return stripped.split(":", 1)[1].strip()[:500]
    return ""


def worker_main(run_id: str, config_path: Path | None = None) -> int:
    """Entry point for ``agent-duet worker --run-id UUID``."""
    config = load_config(config_path)
    ensure_state_dirs(config)
    log_file = setup_logging(
        "worker", state_dir=config.state_path, level=config.log_level, to_stderr=True
    )
    store = StateStore(config.db_path)
    record = store.get(run_id)
    logger.info(
        "worker starting: run=%s pid=%d repo=%s mode=%s base=%s run_dir=%s log=%s",
        run_id,
        os.getpid(),
        record.repo_path,
        record.delivery_mode,
        record.base_sha,
        record.run_dir,
        log_file,
    )
    logger.info(
        "child access posture: claude=%s codex_sandbox=%s env_mode=%s codex_write_policy=%s",
        "dangerously-skip-permissions"
        if config.claude.dangerously_skip_permissions
        else config.claude.permission_mode,
        config.codex.sandbox_mode,
        config.child_env_mode,
        config.codex.write_policy,
    )
    worker = Worker(config=config, store=store, run_id=run_id)
    asyncio.run(worker.execute())
    final = store.get(run_id)
    logger.info(
        "worker finished: run=%s phase=%s summary=%s error=%s",
        run_id,
        final.phase.value,
        final.summary,
        final.error,
    )
    return 0 if final.phase is not Phase.FAILED else 1


def record_start_state(store: StateStore, run_id: str, remotes: dict[str, str]) -> None:
    """Persist the remote set observed at start, for later tamper checks."""
    store.merge_evidence(run_id, {"start_remotes": remotes})


__all__ = [
    "Cancelled",
    "PhaseFailure",
    "Worker",
    "record_start_state",
    "run_git",
    "worker_main",
]
