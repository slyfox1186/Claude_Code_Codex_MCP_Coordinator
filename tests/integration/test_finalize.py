#!/usr/bin/env python3
"""End-to-end coverage of duet_finalize, the only tool that publishes anything.

Every test drives the real MCP function against a real bare remote, so the assertions
are about actual git object ids rather than about what the code claims it did.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from agent_duet.artifacts import CRITIQUE_FILENAME, HANDOFF_FILENAME
from agent_duet.git_guard import combined_diff_sha256, owned_tree_sha
from agent_duet.models import Phase, StartRequest
from agent_duet.server import RUNTIME, ToolError, create_run, duet_finalize
from agent_duet.worker import Worker

from helpers import git


@pytest.fixture
def runtime(config, store):
    """Point the module-level runtime at the test config, as the server does."""
    RUNTIME._config, RUNTIME._store = config, store
    yield config, store
    RUNTIME.reset()


def ready_run(config, store, repo, **kwargs):
    """Drive a full run to AWAITING_FINALIZE and return its record."""
    request = StartRequest(
        repo_path=repo,
        task=kwargs.pop("task", "Add a pure function named add(a, b) and tests."),
        acceptance_criteria=["existing tests still pass"],
        **kwargs,
    )
    record = create_run(config, store, request)
    asyncio.run(Worker(config=config, store=store, run_id=record.run_id).execute())
    final = store.get(record.run_id)
    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    return final


def finalize(record, remote_url, **kwargs):
    return asyncio.run(
        duet_finalize(
            run_id=record.run_id,
            expected_branch=record.branch,
            expected_remote_url=str(remote_url),
            commit_message=kwargs.pop("commit_message", "Add add() with tests"),
            **kwargs,
        )
    )


# ---------------------------------------------------------------------------
# The happy path, proved with real object ids
# ---------------------------------------------------------------------------


def test_finalize_commits_pushes_and_verifies_the_remote_sha(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    result = finalize(record, bare_remote)

    assert result.phase is Phase.COMPLETE
    assert result.terminal is True
    assert result.pushed is True
    assert len(result.local_commit_sha) == 40
    assert result.remote_commit_sha == result.local_commit_sha

    # Ask the remote itself, rather than trusting the push's exit code.
    listed = git("ls-remote", str(bare_remote), f"refs/heads/{record.branch}", cwd=repo)
    assert listed.stdout.split()[0] == result.local_commit_sha
    assert store.get(record.run_id).phase is Phase.COMPLETE


def test_finalize_commits_only_run_owned_paths(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    result = finalize(record, bare_remote)
    assert sorted(result.staged_paths) == ["impl.py", "test_impl.py"]
    shown = git(
        "show",
        "--name-only",
        "--pretty=format:",
        result.local_commit_sha,
        cwd=Path(record.worktree),
    )
    assert sorted(name for name in shown.stdout.split() if name) == [
        "impl.py",
        "test_impl.py",
    ]


def test_a_stray_file_appearing_after_validation_blocks_the_commit(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    """An untracked file added after validation changes the fingerprint, so finalize
    refuses outright rather than quietly committing a subset of a changed tree."""
    record = ready_run(config, store, repo)
    (Path(record.worktree) / "stray.txt").write_text("not part of this run\n")
    with pytest.raises(ToolError, match="changed after validation"):
        finalize(record, bare_remote)
    listed = git("ls-remote", str(bare_remote), f"refs/heads/{record.branch}", cwd=repo)
    assert listed.stdout.strip() == ""


def test_only_run_owned_paths_are_ever_staged(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    """`git add -A` is never used: the staged set equals the recorded owned set."""
    record = ready_run(config, store, repo)
    result = finalize(record, bare_remote)
    assert result.staged_paths == sorted(record.owned_paths)


def test_the_commit_contains_no_coordination_artifacts(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    result = finalize(record, bare_remote)
    tree = git(
        "ls-tree", "-r", "--name-only", result.local_commit_sha, cwd=Path(record.worktree)
    )
    assert HANDOFF_FILENAME not in tree.stdout
    assert CRITIQUE_FILENAME not in tree.stdout


def test_finalize_without_push_commits_locally_only(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    result = finalize(record, bare_remote, push=False)
    assert result.phase is Phase.COMPLETE
    assert result.pushed is False
    assert result.remote_commit_sha is None
    listed = git("ls-remote", str(bare_remote), f"refs/heads/{record.branch}", cwd=repo)
    assert listed.stdout.strip() == "", "nothing should have been pushed"


def test_deployment_is_reported_not_checked_without_a_verifier(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    result = finalize(record, bare_remote)
    assert result.deployment.status == "NOT_CHECKED"
    assert result.deployment.deployed_sha is None


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_finalize_refuses_a_run_that_is_not_awaiting_finalize(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    store.transition(record.run_id, Phase.CANCELLED, reason="operator stopped it")
    with pytest.raises(ToolError, match="not AWAITING_FINALIZE"):
        finalize(record, bare_remote)


def test_finalize_refuses_the_wrong_branch(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    with pytest.raises(ToolError, match="branch mismatch"):
        asyncio.run(
            duet_finalize(
                run_id=record.run_id,
                expected_branch="main",
                expected_remote_url=str(bare_remote),
                commit_message="wrong branch",
            )
        )


def test_finalize_refuses_the_wrong_remote_url(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    with pytest.raises(ToolError, match="remote URL mismatch"):
        finalize(record, "git@github.com:someone/else.git")


def test_finalize_refuses_a_remote_name_outside_the_allowlist(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    git("remote", "add", "upstream", str(bare_remote), cwd=Path(record.worktree))
    with pytest.raises(ToolError, match="allowed_remote_names"):
        finalize(record, bare_remote, expected_remote_name="upstream")


def test_finalize_refuses_a_missing_remote(runtime, config, store, repo, fake_log_dir):
    record = ready_run(config, store, repo)
    git("remote", "remove", "origin", cwd=repo)
    with pytest.raises(ToolError, match="does not exist"):
        finalize(record, "git@github.com:example/example.git")


def test_finalize_refuses_after_the_working_tree_changes(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    (Path(record.worktree) / "impl.py").write_text("def add(a, b):\n    return a - b\n")
    with pytest.raises(ToolError, match="changed after validation"):
        finalize(record, bare_remote)


def test_finalize_refuses_a_symlink_retarget_the_diff_cannot_see(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    """The tree-id gate exists for exactly this: identical patch, different object."""
    record = ready_run(config, store, repo)
    worktree = Path(record.worktree)
    base, owned = record.base_sha, list(record.owned_paths)
    index = Path(record.run_dir) / "probe.index"

    (worktree / "impl.py").unlink()
    (worktree / "same_a.py").write_text("def add(a, b):\n    return a + b\n")
    (worktree / "same_b.py").write_text("def add(a, b):\n    return a + b\n")
    os.symlink("same_a.py", worktree / "impl.py")
    diff_before = combined_diff_sha256(worktree, base)
    tree_before = owned_tree_sha(worktree, base, owned, index)

    os.remove(worktree / "impl.py")
    os.symlink("same_b.py", worktree / "impl.py")
    assert combined_diff_sha256(worktree, base) == diff_before, "the diff is blind here"
    assert owned_tree_sha(worktree, base, owned, index) != tree_before

    with pytest.raises(ToolError):
        finalize(record, bare_remote)
    assert store.get(record.run_id).phase is not Phase.COMPLETE


def test_finalize_refuses_a_remote_rewritten_after_validation(
    runtime, config, store, repo, bare_remote, fake_log_dir, work_root
):
    """Even when the caller supplies the NEW url, the immutable start record wins."""
    record = ready_run(config, store, repo)
    evil = work_root / "evil-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(evil)], check=True)
    git("remote", "set-url", "origin", str(evil), cwd=repo)

    with pytest.raises(ToolError, match="changed during the run"):
        finalize(record, evil)
    listed = git("ls-remote", str(evil), "refs/heads/*", cwd=repo)
    assert listed.stdout.strip() == "", "nothing may reach the substituted remote"


def test_finalize_refuses_a_credential_in_the_commit_set(
    runtime, config, store, repo, bare_remote, fake_log_dir, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "secret")
    record = ready_run(config, store, repo)
    with pytest.raises(ToolError, match="credential-shaped"):
        finalize(record, bare_remote)
    listed = git("ls-remote", str(bare_remote), f"refs/heads/{record.branch}", cwd=repo)
    assert listed.stdout.strip() == ""


def test_finalize_refuses_a_leftover_artifact_in_the_worktree(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    record = ready_run(config, store, repo)
    (Path(record.worktree) / CRITIQUE_FILENAME).write_text("snuck back in\n")
    # Either gate is acceptable; what matters is that it never reaches a commit.
    with pytest.raises(ToolError, match=r"still present|changed after validation"):
        finalize(record, bare_remote)
    listed = git("ls-remote", str(bare_remote), f"refs/heads/{record.branch}", cwd=repo)
    assert listed.stdout.strip() == ""


def test_a_refused_finalize_leaves_the_run_finalizable(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    """A precondition refusal must not strand the run in FINALIZING."""
    record = ready_run(config, store, repo)
    with pytest.raises(ToolError):
        finalize(record, "git@github.com:someone/else.git")
    assert store.get(record.run_id).phase is Phase.AWAITING_FINALIZE
    assert finalize(record, bare_remote).phase is Phase.COMPLETE


def test_finalize_is_not_repeatable(runtime, config, store, repo, bare_remote, fake_log_dir):
    record = ready_run(config, store, repo)
    finalize(record, bare_remote)
    with pytest.raises(ToolError, match="not AWAITING_FINALIZE"):
        finalize(record, bare_remote)


def test_a_run_that_changed_nothing_cannot_be_committed(
    runtime, config, store, repo, bare_remote, fake_log_dir
):
    """An empty commit set is refused rather than producing an empty commit."""
    record = ready_run(config, store, repo)
    store.update(record.run_id, owned_paths=[])
    with pytest.raises(ToolError, match="nothing to commit"):
        finalize(store.get(record.run_id), bare_remote)
