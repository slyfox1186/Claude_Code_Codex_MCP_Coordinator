#!/usr/bin/env python3
"""Repository guard, remote-URL normalization, locking, and fingerprints."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest
from agent_duet.git_guard import (
    GitError,
    RepoLockedError,
    add_worktree,
    changed_paths,
    combined_diff_sha256,
    fingerprint,
    inspect_repo,
    lock_path_for,
    normalize_remote_url,
    repo_lock,
    stage_paths,
    staged_paths,
)

from helpers import git

# -- inspection -------------------------------------------------------------


def test_inspect_reports_exact_state(repo):
    info = inspect_repo(repo)
    assert info.path == repo.resolve()
    assert info.toplevel == repo.resolve()
    assert info.branch == "main"
    assert info.detached is False
    assert len(info.head_sha) == 40
    assert info.remotes["origin"] == "git@github.com:example/example.git"
    assert info.clean is True


def test_inspect_rejects_a_subdirectory(repo):
    sub = repo / "src"
    sub.mkdir()
    with pytest.raises(GitError, match="not the repository root"):
        inspect_repo(sub)


def test_inspect_rejects_a_dot_git_directory(repo):
    with pytest.raises(GitError, match=r"refusing to operate on a \.git directory"):
        inspect_repo(repo / ".git")


def test_inspect_rejects_a_non_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError):
        inspect_repo(plain)


def test_inspect_rejects_the_home_directory():
    with pytest.raises(GitError, match="refusing to operate"):
        inspect_repo(Path.home())


def test_inspect_detects_a_dirty_tree(repo):
    (repo / "dirty.txt").write_text("x\n")
    assert inspect_repo(repo).clean is False


def test_untracked_file_counts_as_dirty(repo):
    (repo / "untracked.txt").write_text("x\n")
    assert inspect_repo(repo).clean is False


def test_inspect_detects_detached_head(repo):
    git("checkout", "-q", "--detach", cwd=repo)
    info = inspect_repo(repo)
    assert info.detached is True
    assert info.branch is None


def test_common_dir_is_shared_by_worktrees(repo, tmp_path):
    worktree = tmp_path / "wt"
    add_worktree(repo, worktree, "agent-duet/test", inspect_repo(repo).head_sha)
    assert inspect_repo(worktree).git_common_dir == inspect_repo(repo).git_common_dir


# -- remote URL normalization ----------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("git@github.com:Org/Repo.git", "https://github.com/org/repo"),
        ("ssh://git@github.com/org/repo.git", "git@github.com:org/repo"),
        ("https://github.com/org/repo/", "https://github.com/org/repo.git"),
        ("https://user:token@github.com/org/repo.git", "git@github.com:org/repo.git"),
        ("https://github.com:443/org/repo.git", "https://github.com/org/repo"),
    ],
)
def test_equivalent_remote_urls_normalize_equal(left, right):
    assert normalize_remote_url(left) == normalize_remote_url(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("git@github.com:org/repo.git", "git@github.com:org/other.git"),
        ("git@github.com:org/repo.git", "git@gitlab.com:org/repo.git"),
        ("git@github.com:org/repo.git", "git@github.com:evil/repo.git"),
    ],
)
def test_different_remotes_do_not_normalize_equal(left, right):
    assert normalize_remote_url(left) != normalize_remote_url(right)


def test_normalizing_strips_credentials():
    assert "token" not in normalize_remote_url("https://user:token@github.com/o/r.git")


def test_empty_url_normalizes_to_empty():
    assert normalize_remote_url("   ") == ""


# -- locking ----------------------------------------------------------------


def test_lock_is_keyed_by_common_dir_not_worktree(tmp_path, repo):
    locks = tmp_path / "locks"
    worktree = tmp_path / "wt"
    add_worktree(repo, worktree, "agent-duet/lock", inspect_repo(repo).head_sha)
    a = lock_path_for(locks, inspect_repo(repo).git_common_dir)
    b = lock_path_for(locks, inspect_repo(worktree).git_common_dir)
    assert a == b


def _hold_lock(locks_dir: str, common_dir: str, ready, release):
    with repo_lock(Path(locks_dir), Path(common_dir)):
        ready.set()
        release.wait(30)


def test_second_process_cannot_take_the_lock(tmp_path, repo):
    locks = tmp_path / "locks"
    common = inspect_repo(repo).git_common_dir
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(locks), str(common), ready, release))
    holder.start()
    try:
        assert ready.wait(30), "the holder process never acquired the lock"
        with (
            pytest.raises(RepoLockedError, match="another agent_duet writer"),
            repo_lock(locks, common),
        ):
            pass
    finally:
        release.set()
        holder.join(30)
    # Once the holder exits, the lock is available again.
    with repo_lock(locks, common):
        pass


def test_lock_file_is_private(tmp_path, repo):
    locks = tmp_path / "locks"
    common = inspect_repo(repo).git_common_dir
    with repo_lock(locks, common) as path:
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_lock_is_reentrant_after_release(tmp_path, repo):
    locks = tmp_path / "locks"
    common = inspect_repo(repo).git_common_dir
    for _ in range(3):
        with repo_lock(locks, common):
            pass


# -- fingerprints -----------------------------------------------------------


def test_fingerprint_is_stable_when_nothing_changes(repo):
    assert fingerprint(repo).differences(fingerprint(repo)) == []


def test_fingerprint_detects_a_new_file(repo):
    before = fingerprint(repo)
    (repo / "new.txt").write_text("hello\n")
    assert "working tree status changed" in before.differences(fingerprint(repo))


def test_fingerprint_detects_an_edit(repo):
    (repo / "README.md").write_text("# example\nmore\n")
    before = fingerprint(repo)
    (repo / "README.md").write_text("# example\neven more\n")
    assert "unstaged diff changed" in before.differences(fingerprint(repo))


def test_fingerprint_detects_staging(repo):
    (repo / "new.txt").write_text("hello\n")
    before = fingerprint(repo)
    git("add", "new.txt", cwd=repo)
    assert "staged diff changed" in before.differences(fingerprint(repo))


def test_fingerprint_detects_a_commit(repo):
    (repo / "new.txt").write_text("hello\n")
    git("add", "new.txt", cwd=repo)
    before = fingerprint(repo)
    git("commit", "-q", "-m", "x", cwd=repo)
    assert any("HEAD" in item for item in before.differences(fingerprint(repo)))


def test_fingerprint_detects_a_new_remote(repo):
    before = fingerprint(repo)
    git("remote", "add", "extra", "https://example.invalid/x.git", cwd=repo)
    assert any("remotes changed" in item for item in before.differences(fingerprint(repo)))


def test_fingerprint_detects_a_branch_switch(repo):
    before = fingerprint(repo)
    git("checkout", "-q", "-b", "other", cwd=repo)
    assert any("branch" in item for item in before.differences(fingerprint(repo)))


# -- combined diff and owned paths -----------------------------------------


def test_combined_diff_covers_untracked_files(repo):
    base = inspect_repo(repo).head_sha
    first = combined_diff_sha256(repo, base)
    (repo / "brand_new.py").write_text("x = 1\n")
    assert combined_diff_sha256(repo, base) != first


def test_combined_diff_notices_untracked_content_change(repo):
    base = inspect_repo(repo).head_sha
    (repo / "brand_new.py").write_text("x = 1\n")
    first = combined_diff_sha256(repo, base)
    (repo / "brand_new.py").write_text("x = 2\n")
    assert combined_diff_sha256(repo, base) != first


def test_combined_diff_is_stable_for_an_unchanged_tree(repo):
    base = inspect_repo(repo).head_sha
    (repo / "a.py").write_text("x = 1\n")
    assert combined_diff_sha256(repo, base) == combined_diff_sha256(repo, base)


def test_changed_paths_lists_tracked_and_untracked(repo):
    base = inspect_repo(repo).head_sha
    (repo / "README.md").write_text("# changed\n")
    (repo / "added.py").write_text("y = 2\n")
    assert changed_paths(repo, base) == ["README.md", "added.py"]


def test_changed_paths_ignores_gitignored_files(repo):
    (repo / ".gitignore").write_text("ignored.txt\n")
    git("add", ".gitignore", cwd=repo)
    git("commit", "-q", "-m", "ignore", cwd=repo)
    base = inspect_repo(repo).head_sha
    (repo / "ignored.txt").write_text("noise\n")
    assert changed_paths(repo, base) == []


# -- staging ---------------------------------------------------------------


def test_stage_paths_stages_only_what_it_is_given(repo):
    (repo / "wanted.py").write_text("a = 1\n")
    (repo / "unwanted.py").write_text("b = 2\n")
    stage_paths(repo, ["wanted.py"])
    assert staged_paths(repo) == ["wanted.py"]


def test_stage_paths_refuses_an_empty_list(repo):
    with pytest.raises(GitError, match="no run-owned paths"):
        stage_paths(repo, [])


def test_stage_paths_treats_a_dash_prefixed_name_as_a_path(repo):
    weird = repo / "-weird.py"
    weird.write_text("c = 3\n")
    stage_paths(repo, ["-weird.py"])
    assert staged_paths(repo) == ["-weird.py"]
    os.unlink(weird)
