#!/usr/bin/env python3
"""Regression tests for defects found by independent review.

Each test here failed against the implementation before its fix. They are grouped by
the defect they pin down so a future change that reopens one fails loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agent_duet.artifacts import CRITIQUE_FILENAME, atomic_write_text
from agent_duet.config import ConfigError, ensure_state_dirs, load_config
from agent_duet.git_guard import changed_paths, combined_diff_sha256, owned_tree_sha
from agent_duet.models import Evidence, Phase, StartRequest
from agent_duet.process_guard import process_alive, terminate_process_group
from agent_duet.server import (
    ToolError,
    _reap_dead_runs,
    _status_with_liveness,
    _worker_vanished,
    create_run,
)
from agent_duet.state import StateError, StateStore
from agent_duet.worker import Worker

from helpers import git


def start(config, store, repo, **kwargs):
    request = StartRequest(
        repo_path=repo,
        task=kwargs.pop("task", "Add a pure function named add(a, b) and tests."),
        acceptance_criteria=kwargs.pop("acceptance_criteria", ["existing tests still pass"]),
        **kwargs,
    )
    return create_run(config, store, request)


def run_worker(config, store, run_id) -> None:
    asyncio.run(Worker(config=config, store=store, run_id=run_id).execute())


# ---------------------------------------------------------------------------
# Defect: stored evidence could not be projected into the MCP status shape
# ---------------------------------------------------------------------------


def test_private_evidence_keys_do_not_break_the_status_projection(store, tmp_path, repo, config):
    """`start_remotes` is internal bookkeeping and must not reach the public schema."""
    record = start(config, store, repo)
    store.merge_evidence(
        record.run_id,
        {"start_remotes": {"origin": "git@example.invalid:o/r.git"}, "not_a_field": 7},
    )
    status = store.get(record.run_id).to_status()
    assert status.evidence.model_dump().get("start_remotes") is None
    assert "not_a_field" not in status.evidence.model_dump()


def test_every_key_the_worker_writes_survives_projection(config, store, repo, fake_log_dir):
    """A completed run must be projectable; this failed for three separate keys."""
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    status = store.get(record.run_id).to_status()  # must not raise
    assert status.phase is Phase.AWAITING_FINALIZE
    assert status.evidence.changed_path_count == 2
    assert status.evidence.validated_tree_sha


def test_evidence_from_record_is_total_over_arbitrary_blobs():
    evidence = Evidence.from_record({"anything": 1, "critique_archived": True})
    assert evidence.critique_archived is True


# ---------------------------------------------------------------------------
# Defect: cancelling killed the worker but orphaned the child agent
# ---------------------------------------------------------------------------


WORKER_TOPOLOGY = """
import asyncio, os, subprocess, sys

async def main():
    child = await asyncio.create_subprocess_exec(
        "/usr/bin/sleep", "300",
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"{os.getpid()} {os.getpgid(0)} {child.pid} {os.getpgid(child.pid)}", flush=True)
    await asyncio.sleep(300)

asyncio.run(main())
"""


@pytest.fixture
def worker_topology():
    """Spawn the real topology: a detached worker whose child is in its own group."""
    process = subprocess.Popen(
        [sys.executable, "-c", WORKER_TOPOLOGY],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    worker_pid, worker_pgid, child_pid, child_pgid = (
        int(value) for value in process.stdout.readline().split()
    )
    yield worker_pid, worker_pgid, child_pid, child_pgid
    for pid in (child_pid, worker_pid):
        with contextlib.suppress(OSError):
            os.kill(pid, 9)


def test_a_child_agent_is_not_in_the_workers_process_group(worker_topology):
    """This is why cancelling has to signal the child explicitly."""
    _, worker_pgid, _, child_pgid = worker_topology
    assert worker_pgid != child_pgid


def test_killing_only_the_worker_group_orphans_the_child(worker_topology):
    """Pins the original defect: the naive reap leaves a privileged agent running."""
    worker_pid, worker_pgid, child_pid, _ = worker_topology
    terminate_process_group(worker_pgid, grace_seconds=2)
    time.sleep(0.5)
    assert not process_alive(worker_pid, None)
    assert process_alive(child_pid, None), "the child survives a worker-only reap"


def test_reaping_the_child_group_first_leaves_nothing_behind(worker_topology):
    """The fix: signal the recorded child group, then the worker."""
    worker_pid, worker_pgid, child_pid, child_pgid = worker_topology
    terminate_process_group(child_pgid, pid=child_pid, grace_seconds=2)
    terminate_process_group(worker_pgid, pid=worker_pid, grace_seconds=2)
    time.sleep(0.5)
    assert not process_alive(child_pid, None)
    assert not process_alive(worker_pid, None)


def test_cancel_reaps_the_recorded_child_group(config, store, repo, monkeypatch):
    """duet_cancel must terminate the child agent recorded by the worker."""
    from agent_duet.server import _reap_run_processes

    record = start(config, store, repo)
    child = subprocess.Popen(
        ["/usr/bin/sleep", "300"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    from agent_duet.process_guard import process_start_ticks

    store.set_active_child(
        record.run_id,
        pid=child.pid,
        pgid=os.getpgid(child.pid),
        ticks=process_start_ticks(child.pid),
        label="phase1-claude",
    )
    outcomes = _reap_run_processes(store, store.get(record.run_id))
    time.sleep(0.5)
    assert any("phase1-claude" in item for item in outcomes)
    assert not process_alive(child.pid, None)
    assert store.get(record.run_id).active_child_pid is None


def test_the_worker_records_and_clears_its_active_child(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.active_child_pid is None, "a finished run must not claim a live child"
    assert final.active_child_pgid is None


# ---------------------------------------------------------------------------
# Defect: the validated baseline could be silently replaced
# ---------------------------------------------------------------------------


def test_head_moving_between_start_and_worker_fails_the_run(config, store, repo, fake_log_dir):
    """The recorded base is authoritative; the run must refuse to rebase itself."""
    record = start(config, store, repo)
    (repo / "someone_else.txt").write_text("a commit from outside the run\n")
    git("add", "someone_else.txt", cwd=repo)
    git("commit", "-q", "-m", "outside commit", cwd=repo)

    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "HEAD moved" in (final.error or "")
    assert final.base_sha == record.base_sha, "the recorded baseline must not change"


def test_remotes_changing_between_start_and_worker_fails_the_run(
    config, store, repo, fake_log_dir
):
    record = start(config, store, repo)
    git("remote", "add", "sneaky", "https://example.invalid/x.git", cwd=repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "remotes changed" in (final.error or "")


def test_the_worktree_is_created_from_the_recorded_base(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    merge_base = git("rev-parse", "HEAD", cwd=Path(final.worktree)).stdout.strip()
    assert merge_base == record.base_sha


# ---------------------------------------------------------------------------
# Defect: remote rewrites during a write-capable phase were not caught
# ---------------------------------------------------------------------------


def test_a_remote_rewritten_during_reconciliation_fails_the_run(
    config, store, repo, fake_log_dir, monkeypatch
):
    """Phase 3 is write-capable, so the remote set is re-checked after it."""
    monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "reconcile_add_remote")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "remotes changed during reconciliation" in (final.error or "")


# ---------------------------------------------------------------------------
# Defect: the diff digest could not see a symlink retarget
# ---------------------------------------------------------------------------


def test_the_diff_digest_alone_is_blind_to_a_symlink_retarget(repo):
    """Documents exactly why the tree id exists; a diff-only gate would pass here."""
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "a.txt").write_text("same\n")
    (repo / "b.txt").write_text("same\n")
    os.symlink("a.txt", repo / "link")

    before = combined_diff_sha256(repo, base)
    os.remove(repo / "link")
    os.symlink("b.txt", repo / "link")
    assert combined_diff_sha256(repo, base) == before, "the textual diff cannot see this"


def test_the_tree_id_does_see_a_symlink_retarget(repo, tmp_path):
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "a.txt").write_text("same\n")
    (repo / "b.txt").write_text("same\n")
    os.symlink("a.txt", repo / "link")

    owned = changed_paths(repo, base)
    before = owned_tree_sha(repo, base, owned, tmp_path / "idx")
    os.remove(repo / "link")
    os.symlink("b.txt", repo / "link")
    after = owned_tree_sha(repo, base, owned, tmp_path / "idx")
    assert before != after


def test_the_tree_id_sees_an_executable_bit_change(repo, tmp_path):
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    owned = changed_paths(repo, base)
    before = owned_tree_sha(repo, base, owned, tmp_path / "idx")
    script.chmod(0o755)
    assert owned_tree_sha(repo, base, owned, tmp_path / "idx") != before


def test_the_tree_id_sees_binary_content_change(repo, tmp_path):
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "blob.bin").write_bytes(b"\x00\x01A" * 50)
    owned = changed_paths(repo, base)
    before = owned_tree_sha(repo, base, owned, tmp_path / "idx")
    (repo / "blob.bin").write_bytes(b"\x00\x01B" * 50)
    assert owned_tree_sha(repo, base, owned, tmp_path / "idx") != before


def test_the_tree_id_is_stable_for_an_unchanged_tree(repo, tmp_path):
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "a.txt").write_text("content\n")
    owned = changed_paths(repo, base)
    first = owned_tree_sha(repo, base, owned, tmp_path / "idx")
    assert owned_tree_sha(repo, base, owned, tmp_path / "idx") == first


def test_owned_tree_sha_does_not_disturb_the_real_index(repo, tmp_path):
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "a.txt").write_text("content\n")
    owned_tree_sha(repo, base, changed_paths(repo, base), tmp_path / "idx")
    assert git("diff", "--cached", "--name-only", cwd=repo).stdout.strip() == ""


# ---------------------------------------------------------------------------
# Defect: read-only tools mutated the database
# ---------------------------------------------------------------------------


def test_status_reports_a_dead_worker_without_writing(config, store, repo):
    """duet_status is annotated read-only, so it must not persist the repair."""
    record = start(config, store, repo)
    store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING)
    store.update(record.run_id, worker_pid=999_999_999, worker_start_ticks="123")

    stale = store.get(record.run_id)
    assert _worker_vanished(stale)
    status = _status_with_liveness(stale)
    assert status.phase is Phase.FAILED
    assert status.terminal is True
    assert store.get(record.run_id).phase is Phase.CLAUDE_IMPLEMENTING, "no write allowed"


def test_start_reaps_a_crashed_run_so_it_stops_blocking(config, store, repo):
    record = start(config, store, repo)
    store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING)
    store.update(record.run_id, worker_pid=999_999_999, worker_start_ticks="123")

    reaped = _reap_dead_runs(store, str(repo))
    assert reaped == [record.run_id]
    assert store.get(record.run_id).phase is Phase.FAILED
    assert store.active_runs(str(repo)) == []


def test_reaping_leaves_a_live_run_alone(config, store, repo):
    record = start(config, store, repo)
    store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING)
    store.update(record.run_id, worker_pid=os.getpid(), worker_start_ticks=None)
    assert _reap_dead_runs(store, str(repo)) == []
    assert store.get(record.run_id).phase is Phase.CLAUDE_IMPLEMENTING


# ---------------------------------------------------------------------------
# Defect: unsafe configuration and artifact writes
# ---------------------------------------------------------------------------


def test_a_symlinked_config_is_refused(config_file, tmp_path):
    link = tmp_path / "linked-config.toml"
    link.symlink_to(config_file)
    with pytest.raises(ConfigError, match="symlink"):
        load_config(link)


def test_a_group_writable_config_is_refused(config_file):
    config_file.chmod(0o660)
    try:
        with pytest.raises(ConfigError, match="group- or world-writable"):
            load_config(config_file)
    finally:
        config_file.chmod(0o600)


def test_a_private_config_is_accepted(config_file):
    config_file.chmod(0o600)
    assert load_config(config_file) is not None


def test_atomic_write_refuses_a_symlinked_destination(tmp_path):
    """A pre-created symlink must not redirect a coordinator write."""
    outside = tmp_path / "outside.txt"
    outside.write_text("original\n")
    target = tmp_path / "artifact.md"
    target.symlink_to(outside)
    with pytest.raises(Exception, match="symlink"):
        atomic_write_text(target, "new content\n")
    assert outside.read_text() == "original\n"


def test_atomic_write_still_replaces_a_regular_file(tmp_path):
    target = tmp_path / "artifact.md"
    atomic_write_text(target, "first\n")
    atomic_write_text(target, "second\n")
    assert target.read_text() == "second\n"
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_no_temp_files_are_left_behind(tmp_path):
    atomic_write_text(tmp_path / "artifact.md", "x\n")
    assert [p.name for p in tmp_path.iterdir()] == ["artifact.md"]


# ---------------------------------------------------------------------------
# Defect: the critique byte contract was ambiguous
# ---------------------------------------------------------------------------


def test_the_archived_critique_matches_the_captured_message_exactly(
    config, store, repo, fake_log_dir
):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    archived = (Path(final.run_dir) / "artifacts" / CRITIQUE_FILENAME).read_text()
    captured = (Path(final.run_dir) / "phase2.codex_last_message.md").read_text()
    assert archived == captured.rstrip("\n") + "\n"
    assert archived.endswith("\n")
    assert not archived.endswith("\n\n")


def test_the_recorded_digest_matches_the_archived_bytes(config, store, repo, fake_log_dir):
    from agent_duet.artifacts import sha256_text

    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    archived = (Path(final.run_dir) / "artifacts" / CRITIQUE_FILENAME).read_text()
    assert final.evidence["critique_sha256"] == sha256_text(archived)


# ---------------------------------------------------------------------------
# Defect: max_parallel_global was parsed but never enforced
# ---------------------------------------------------------------------------


def test_a_second_concurrent_run_is_refused(config, store, repo, work_root):
    from agent_duet.server import RUNTIME, ToolError, duet_start

    other = work_root / "second"
    other.mkdir()
    git("init", "-q", "-b", "main", cwd=other)
    git("config", "user.email", "t@t.t", cwd=other)
    git("config", "user.name", "t", cwd=other)
    (other / "README.md").write_text("# second\n")
    git("add", "README.md", cwd=other)
    git("commit", "-q", "-m", "initial", cwd=other)

    start(config, store, repo)  # occupies the single global slot
    RUNTIME._config, RUNTIME._store = config, store
    try:
        with pytest.raises(ToolError, match="max_parallel_global"):
            asyncio.run(duet_start(repo_path=str(other), task="anything at all"))
    finally:
        RUNTIME.reset()


# ---------------------------------------------------------------------------
# Observed live: the canonical repository was deleted mid-run by an unrelated
# cleanup process. The run must fail safely and say something useful.
# ---------------------------------------------------------------------------


def test_a_deleted_parent_repository_is_explained_clearly(config, store, repo, tmp_path):
    """A linked worktree whose parent is gone must not report a bare git error."""
    import shutil

    from agent_duet.git_guard import GitError, add_worktree, inspect_repo

    worktree = tmp_path / "orphan-worktree"
    add_worktree(repo, worktree, "agent-duet/orphan", inspect_repo(repo).head_sha)
    shutil.rmtree(repo)

    with pytest.raises(GitError, match="parent repository is gone"):
        inspect_repo(worktree)


def test_a_repository_replaced_by_a_plain_directory_is_explained(tmp_path):
    from agent_duet.git_guard import GitError, inspect_repo

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(GitError, match=r"no longer a git repository|not the repository root"):
        inspect_repo(plain)


def test_losing_the_repository_mid_run_commits_nothing(config, store, repo, fake_log_dir):
    """The whole point: an environment fault fails the run, it never half-publishes."""
    import shutil

    record = start(config, store, repo)
    worktree = config.worktrees_dir / f"{repo.name}-{record.run_id[:8]}" / record.run_id[:8]
    from agent_duet.git_guard import add_worktree, inspect_repo

    add_worktree(repo, worktree, record.branch, inspect_repo(repo).head_sha)
    store.update(record.run_id, worktree=str(worktree))
    shutil.rmtree(repo)

    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert final.error
    assert final.validated_tree_sha is None, "nothing may be marked validated"
    assert final.active_child_pid is None


# --- an anticipated refusal must reach the caller, not just the server log ------


def test_a_refusal_reaches_the_caller_with_its_reason_intact(config, store, repo):
    """Every refusal here is only useful if the model can read *why*.

    ``agent_duet.server.ToolError`` once subclassed ``RuntimeError``. The SDK's tool
    layer treats only its own ``ToolError`` as anticipated; anything else is a crash,
    so the caller received a bare "Error executing tool duet_start" while the reason
    stayed in the server log. Observed live: a session was told nothing at all when a
    prior run held the only slot.
    """
    from mcp.server.mcpserver.exceptions import ToolError as McpToolError

    assert issubclass(ToolError, McpToolError), (
        "ToolError must subclass the SDK's ToolError or its message is withheld"
    )

    start(config, store, repo)  # occupies the single slot
    with pytest.raises(ToolError) as caught:
        start(config, store, repo, task="a second, blocked task")

    message = str(caught.value)
    assert "already active" in message
    assert str(repo) in message, "the refusal must name the repository holding the slot"


async def test_the_sdk_tool_layer_forwards_the_refusal_text(config, store, repo):
    """Prove it end to end through the SDK, not just by inheritance."""
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError as McpToolError

    server = MCPServer("test")

    @server.tool()
    def refusing_tool() -> str:
        raise ToolError("a run is already active for /some/repo; cancel it first")

    with pytest.raises(McpToolError) as caught:
        await server.call_tool("refusing_tool", {})

    assert "a run is already active for /some/repo" in str(caught.value)


def test_a_run_awaiting_finalize_says_so_when_it_blocks_a_start(config, store, repo):
    """The blocking run is usually one the operator forgot to finalize.

    Telling them "wait for it to finish" is wrong in that case: it never will.
    """
    record = start(config, store, repo)
    for phase in (
        Phase.CLAUDE_IMPLEMENTING,
        Phase.HANDOFF_VALIDATING,
        Phase.CODEX_REVIEWING,
        Phase.REVIEW_INTEGRITY_CHECK,
        Phase.CLAUDE_RECONCILING,
        Phase.FINAL_VALIDATING,
        Phase.AWAITING_FINALIZE,
    ):
        store.transition(record.run_id, phase, reason="driven by the test")

    with pytest.raises(ToolError) as caught:
        start(config, store, repo, task="a second, blocked task")

    message = str(caught.value)
    assert str(record.run_id) in message
    assert "duet_finalize" in message and "duet_cancel" in message


# --- a run nobody checked must say so ------------------------------------------


def test_a_repo_with_no_validation_commands_is_reported_as_unvalidated(
    config, store, repo
):
    """"All configured validations passed" reads as a pass when none were configured.

    Observed live: a PowerShell repository was registered with
    ``validation_commands = []`` (nothing the detector recognises), and the run would
    have reached AWAITING_FINALIZE announcing that validation succeeded. Nothing had
    run. The only thing behind the work was what the two models said about it.
    """
    record = create_run(config, store, StartRequest(
        repo_path=repo,
        task="Add a pure function named add(a, b) and tests.",
        acceptance_criteria=["existing tests still pass"],
    ))
    asyncio.run(Worker(config=config, store=store, run_id=record.run_id).execute())
    final = store.get(record.run_id)

    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    assert final.evidence["unvalidated"] is True
    assert final.evidence["validations"] == []
    assert "NO configured validation_commands" in final.summary
    assert "only the agents' own claims" in final.summary

    # And it must survive the public projection, not just sit in the raw row.
    assert Evidence.from_record(final.evidence).unvalidated is True


def test_a_repo_with_validation_commands_is_not_flagged_unvalidated(
    config_file, work_root, repo
):
    """The flag must be about absence of commands, not about them being cheap."""
    text = config_file.read_text().replace(
        "validation_commands = []",
        'validation_commands = [["/bin/true"]]',
    )
    config_file.write_text(text)
    config_file.chmod(0o600)
    reloaded = load_config(config_file)
    ensure_state_dirs(reloaded)
    reloaded_store = StateStore(reloaded.db_path)

    record = create_run(reloaded, reloaded_store, StartRequest(
        repo_path=repo,
        task="Add a pure function named add(a, b) and tests.",
        acceptance_criteria=["existing tests still pass"],
    ))
    asyncio.run(
        Worker(config=reloaded, store=reloaded_store, run_id=record.run_id).execute()
    )
    final = reloaded_store.get(record.run_id)

    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    assert final.evidence["unvalidated"] is False
    assert len(final.evidence["validations"]) == 1
    assert "all 1 configured validation(s) passed" in final.summary


# --- "is it still running?" must be answerable from a shell --------------------


def test_runs_listing_reports_whether_the_worker_is_still_alive(config, store, repo):
    """A client session dying is the moment this question gets asked.

    The worker is detached and outlives its session, but the phase alone cannot show
    that: a crashed run sits at its last phase forever, looking identical to one that
    is still working.
    """
    from agent_duet.diagnostics import _worker_state

    record = start(config, store, repo)
    assert _worker_state(record) == "?", "no recorded pid yet"

    store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING, reason="started")
    live = store.update(record.run_id, worker_pid=os.getpid(), worker_start_ticks=None)
    assert _worker_state(live) == "alive"

    dead = store.update(record.run_id, worker_pid=999_999_999, worker_start_ticks=None)
    assert _worker_state(dead) == "DEAD"

    for phase in (
        Phase.HANDOFF_VALIDATING,
        Phase.CODEX_REVIEWING,
        Phase.REVIEW_INTEGRITY_CHECK,
        Phase.CLAUDE_RECONCILING,
        Phase.FINAL_VALIDATING,
        Phase.AWAITING_FINALIZE,
    ):
        store.transition(record.run_id, phase, reason="driven by the test")
    waiting = store.get(record.run_id)
    assert _worker_state(waiting) == "you", "AWAITING_FINALIZE has no worker by design"

    store.transition(record.run_id, Phase.CANCELLED, reason="done")
    assert _worker_state(store.get(record.run_id)) == "-"


# ---------------------------------------------------------------------------
# gc forgot the database row and git's worktree registration
# ---------------------------------------------------------------------------


def test_gc_forgets_the_row_of_a_run_whose_artifacts_are_already_gone(
    config, store, repo, capsys
):
    """A row used to outlive its artifacts and haunt ``agent-duet runs`` forever.

    gc only ever looked at directories, so once the run directory was gone -- deleted by
    hand, or with the repository it belonged to -- the run became invisible to gc and
    permanent in the listing, pointing at a repository that may no longer exist.
    """
    from agent_duet.server import gc

    record = start(config, store, repo)
    store.transition(record.run_id, Phase.CANCELLED, reason="done")
    shutil.rmtree(record.run_dir, ignore_errors=True)

    assert gc(0, apply=False, config_path=config.source_path) == 0
    assert "no artifacts left on disk" in capsys.readouterr().out
    assert store.get(record.run_id).run_id == record.run_id, "dry run must not delete"

    assert gc(0, apply=True, config_path=config.source_path) == 0
    assert "forgot 1 run(s)" in capsys.readouterr().out
    with pytest.raises(StateError):
        store.get(record.run_id)


def test_gc_never_forgets_a_run_that_is_still_going(config, store, repo, capsys):
    """The cutoff is by age, and a long-running run is old. Terminality is the guard."""
    from agent_duet.server import gc

    record = start(config, store, repo)
    store.transition(record.run_id, Phase.CLAUDE_IMPLEMENTING, reason="started")

    assert gc(0, apply=True, config_path=config.source_path) == 0
    assert "nothing older than the cutoff" in capsys.readouterr().out
    assert store.get(record.run_id).phase is Phase.CLAUDE_IMPLEMENTING
    assert store.delete_runs([record.run_id]) == 0, "delete_runs refuses a live run too"


def test_gc_unregisters_the_worktree_instead_of_orphaning_it(config, store, repo, capsys):
    """Deleting a worktree directory behind git's back rots the real repository.

    git keeps a registration under ``.git/worktrees`` for every worktree it created.
    rmtree leaves that entry pointing at nothing, and it stays there for the life of the
    repository -- so ``git worktree list`` accumulates dead entries no one ever prunes.
    """
    from agent_duet.git_guard import add_worktree, run_git
    from agent_duet.server import gc

    record = start(config, store, repo)
    worktree = config.worktrees_dir / f"{repo.name}-{record.run_id[:8]}" / record.run_id[:8]
    add_worktree(repo, worktree, record.branch, record.base_sha)
    store.update(record.run_id, worktree=str(worktree))
    store.transition(record.run_id, Phase.CANCELLED, reason="done")
    assert str(worktree) in run_git(["worktree", "list"], cwd=repo).stdout

    assert gc(0, apply=True, config_path=config.source_path) == 0
    capsys.readouterr()
    assert not worktree.exists()
    assert str(worktree) not in run_git(["worktree", "list"], cwd=repo).stdout
