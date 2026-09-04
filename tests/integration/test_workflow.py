#!/usr/bin/env python3
"""End-to-end workflow runs driven by the fake CLIs.

These exercise the real worker, the real state machine, and the real git operations.
Only the two model CLIs are stand-ins, and they are the only part that cannot be made
deterministic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from agent_duet.artifacts import CRITIQUE_FILENAME, HANDOFF_FILENAME
from agent_duet.models import Phase, StartRequest
from agent_duet.server import create_run
from agent_duet.state import StateStore
from agent_duet.worker import Worker

from helpers import git


def start(config, store, repo, **kwargs):
    """Create a run record exactly as duet_start would, without spawning a worker."""
    request = StartRequest(
        repo_path=repo,
        task=kwargs.pop("task", "Add a pure function named add(a, b) and tests."),
        acceptance_criteria=kwargs.pop(
            "acceptance_criteria", ["existing tests still pass", "new tests cover both cases"]
        ),
        **kwargs,
    )
    return create_run(config, store, request)


def run_worker(config, store, run_id) -> None:
    asyncio.run(Worker(config=config, store=store, run_id=run_id).execute())


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_full_run_reaches_awaiting_finalize(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)

    final = store.get(record.run_id)
    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    assert final.terminal is False
    assert final.branch.startswith("agent-duet/")
    assert final.validated_diff_sha256
    assert sorted(final.owned_paths) == ["impl.py", "test_impl.py"]


def test_every_phase_is_recorded_in_order(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    phases = [phase for _, phase, _ in store.events(record.run_id)]
    assert phases == [
        "QUEUED",
        "CLAUDE_IMPLEMENTING",
        "HANDOFF_VALIDATING",
        "CODEX_REVIEWING",
        "REVIEW_INTEGRITY_CHECK",
        "CLAUDE_RECONCILING",
        "FINAL_VALIDATING",
        "AWAITING_FINALIZE",
    ]


def test_the_original_repository_is_untouched(config, store, repo, fake_log_dir):
    before = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    assert git("rev-parse", "HEAD", cwd=repo).stdout.strip() == before
    assert git("status", "--porcelain", cwd=repo).stdout.strip() == ""
    assert not (repo / "impl.py").exists()


def test_nothing_is_committed_or_pushed_by_a_run(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    worktree = Path(final.worktree)
    assert git("rev-parse", "HEAD", cwd=worktree).stdout.strip() == final.base_sha
    assert git("log", "--oneline", "-1", cwd=worktree).stdout.strip().endswith("initial")


def test_claude_runs_both_write_phases_and_codex_runs_once(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    assert len(list(fake_log_dir.glob("claude-call-*.json"))) == 2
    assert len(list(fake_log_dir.glob("codex-call-*.json"))) == 1


def test_children_are_marked_as_children(config, store, repo, fake_log_dir):
    """Every child must carry AGENT_DUET_CHILD=1 so it cannot re-enter agent_duet."""
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    calls = sorted(fake_log_dir.glob("*-call-*.json"))
    assert calls
    for call in calls:
        assert json.loads(call.read_text())["child_env"] == "1", call.name


def test_children_receive_mcp_isolation_flags(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    for call in sorted(fake_log_dir.glob("claude-call-*.json")):
        argv = json.loads(call.read_text())["argv"]
        assert "--strict-mcp-config" in argv
        assert "mcp__*" in argv
        mcp_config = Path(argv[argv.index("--mcp-config") + 1])
        assert json.loads(mcp_config.read_text()) == {"mcpServers": {}}
    codex_argv = json.loads(next(fake_log_dir.glob("codex-call-*.json")).read_text())["argv"]
    # The reviewer loads no user config at all, so it cannot reach agent_duet's tools.
    # Adding the enabled=false override on top would define a transport-less server
    # entry and codex would refuse to start.
    assert "--ignore-user-config" in codex_argv
    assert "mcp_servers.agent_duet.enabled=false" not in codex_argv


def test_prompts_are_delivered_on_stdin_not_argv(config, store, repo, fake_log_dir):
    """The task text must never appear in a command line."""
    marker = "UNIQUE-TASK-MARKER-9F3A"
    record = start(config, store, repo, task=f"Add add(a, b). Context: {marker}")
    run_worker(config, store, record.run_id)
    for call in sorted(fake_log_dir.glob("*-call-*.json")):
        payload = json.loads(call.read_text())
        assert marker not in " ".join(payload["argv"]), f"{call.name} leaked the task into argv"
    prompts = [json.loads(c.read_text())["prompt"] for c in fake_log_dir.glob("*-call-*.json")]
    assert any(marker in prompt for prompt in prompts), "the task never reached a child"


def test_children_run_inside_the_worktree(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    worktree = str(Path(store.get(record.run_id).worktree).resolve())
    for call in sorted(fake_log_dir.glob("*-call-*.json")):
        assert json.loads(call.read_text())["cwd"] == worktree


def test_coordination_artifacts_are_archived_and_removed(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    worktree = Path(final.worktree)
    assert not (worktree / HANDOFF_FILENAME).exists()
    assert not (worktree / CRITIQUE_FILENAME).exists()
    archive = Path(final.run_dir) / "artifacts"
    assert (archive / HANDOFF_FILENAME).is_file()
    assert (archive / CRITIQUE_FILENAME).is_file()
    assert final.evidence["critique_archived"] is True


def test_the_coordinator_is_the_only_writer_of_the_critique(config, store, repo, fake_log_dir):
    """Codex never writes its report; the broker does, from the captured message."""
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    archived = (Path(final.run_dir) / "artifacts" / CRITIQUE_FILENAME).read_text()
    captured = (Path(final.run_dir) / "phase2.codex_last_message.md").read_text()
    assert archived == captured
    assert final.evidence["critique_sha256"]
    codex_argv = json.loads(next(fake_log_dir.glob("codex-call-*.json")).read_text())["argv"]
    assert CRITIQUE_FILENAME not in " ".join(codex_argv)


def test_evidence_is_populated(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    evidence = store.get(record.run_id).evidence
    for key in (
        "handoff_sha256",
        "critique_sha256",
        "working_diff_sha256",
        "validation_manifest",
        "codex_readonly_verified",
        "proposed_commit_message",
    ):
        assert key in evidence, key
    assert evidence["proposed_commit_message"].startswith("Add add()")


def test_run_artifacts_are_private(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    run_dir = Path(store.get(record.run_id).run_dir)
    assert oct(run_dir.stat().st_mode & 0o777) == "0o700"
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert oct(path.stat().st_mode & 0o777) in ("0o600", "0o644"), path


def test_status_survives_a_new_process_view(config, store, repo, fake_log_dir):
    """Closing and reopening the client must not lose the run."""
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    reopened = StateStore(config.db_path)
    status = reopened.get(record.run_id).to_status()
    assert status.phase is Phase.AWAITING_FINALIZE
    assert status.evidence.working_diff_sha256
    assert "ask" in status.next_action.lower()


# ---------------------------------------------------------------------------
# Validation commands
# ---------------------------------------------------------------------------


def test_configured_validation_runs_and_is_recorded(tmp_path, work_root, repo, fake_log_dir):
    from agent_duet.config import ensure_state_dirs, load_config

    from helpers import FIXTURE_BIN

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'allowed_repo_roots = ["{work_root}"]\n'
        f'state_dir = "{tmp_path / "state"}"\n'
        f'[claude]\nexecutable = "{FIXTURE_BIN / "fake-claude"}"\n'
        f'[codex]\nexecutable = "{FIXTURE_BIN / "fake-codex"}"\n'
        f'[[repositories]]\npath = "{repo}"\n'
        'validation_commands = [["true"], ["echo", "second check"]]\n'
    )
    config = load_config(config_file)
    ensure_state_dirs(config)
    store = StateStore(config.db_path)
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)

    final = store.get(record.run_id)
    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    results = final.evidence["validations"]
    assert [item["argv"] for item in results] == [["true"], ["echo", "second check"]]
    assert all(item["passed"] for item in results)
    manifest = json.loads(Path(final.evidence["validation_manifest"]).read_text())
    assert len(manifest["results"]) == 2


def test_a_failing_validation_fails_the_run(tmp_path, work_root, repo, fake_log_dir):
    from agent_duet.config import ensure_state_dirs, load_config

    from helpers import FIXTURE_BIN

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'allowed_repo_roots = ["{work_root}"]\n'
        f'state_dir = "{tmp_path / "state"}"\n'
        f'[claude]\nexecutable = "{FIXTURE_BIN / "fake-claude"}"\n'
        f'[codex]\nexecutable = "{FIXTURE_BIN / "fake-codex"}"\n'
        f'[[repositories]]\npath = "{repo}"\n'
        'validation_commands = [["false"]]\n'
    )
    config = load_config(config_file)
    ensure_state_dirs(config)
    store = StateStore(config.db_path)
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)

    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "validation command 0" in final.error
    assert final.validated_diff_sha256 is None


# ---------------------------------------------------------------------------
# Phase gates
# ---------------------------------------------------------------------------


def test_missing_handoff_fails_before_the_review(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "no_handoff")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert HANDOFF_FILENAME in final.error
    assert list(fake_log_dir.glob("codex-call-*.json")) == [], "the review must not have run"


def test_an_implementer_written_critique_is_refused(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "writes_critique")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "only the coordinator may write" in final.error


def test_a_premature_commit_fails_the_run(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "commit")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "moved HEAD" in final.error


def test_adding_a_remote_fails_the_run(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "add_remote")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "remotes changed" in final.error


def test_a_failing_implementer_fails_the_run(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "fail")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "exited 3" in final.error


def test_unparseable_implementer_output_fails_the_run(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "bad_json")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    assert store.get(record.run_id).phase is Phase.FAILED


def test_a_failing_reviewer_fails_the_run(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_BEHAVIOR", "fail")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "exited 4" in final.error


def test_an_incomplete_review_turn_fails_the_run(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_BEHAVIOR", "no_completion")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "turn-completed" in final.error


def test_a_stub_review_is_refused(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_BEHAVIOR", "short")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "implausibly short" in final.error


def test_a_review_missing_sections_is_refused(config, store, repo, fake_log_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_BEHAVIOR", "missing_sections")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "missing required sections" in final.error


def test_a_credential_in_the_review_is_redacted_not_propagated(
    config, store, repo, fake_log_dir, monkeypatch
):
    monkeypatch.setenv("FAKE_CODEX_BEHAVIOR", "secret")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    assert final.evidence["critique_redacted"] is True
    archived = (Path(final.run_dir) / "artifacts" / CRITIQUE_FILENAME).read_text()
    assert "ghp_" not in archived
    assert "REDACTED" in archived


# ---------------------------------------------------------------------------
# Reviewer write detection
# ---------------------------------------------------------------------------


def test_a_reviewer_mutation_is_detected_and_recorded(config, store, repo, fake_log_dir, monkeypatch):
    """With the shipped unsandboxed posture, a write is recorded as evidence."""
    monkeypatch.setenv("FAKE_CODEX_BEHAVIOR", "mutate")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    assert final.evidence["codex_readonly_verified"] is False
    assert final.evidence["codex_mutations_detected"]
    reconcile_prompt = (Path(final.run_dir) / "phase3.prompt.md").read_text()
    assert "reviewer ran with write access" in reconcile_prompt


def test_write_policy_fail_aborts_on_a_reviewer_mutation(
    tmp_path, work_root, repo, fake_log_dir, monkeypatch
):
    from agent_duet.config import ensure_state_dirs, load_config

    from helpers import FIXTURE_BIN

    monkeypatch.setenv("FAKE_CODEX_BEHAVIOR", "mutate")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'allowed_repo_roots = ["{work_root}"]\n'
        f'state_dir = "{tmp_path / "state"}"\n'
        f'[claude]\nexecutable = "{FIXTURE_BIN / "fake-claude"}"\n'
        f'[codex]\nexecutable = "{FIXTURE_BIN / "fake-codex"}"\n'
        'sandbox_mode = "read-only"\nwrite_policy = "fail"\n'
    )
    config = load_config(config_file)
    ensure_state_dirs(config)
    store = StateStore(config.db_path)
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "mutated the repository" in final.error


def test_a_clean_review_records_the_read_only_verdict(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.evidence["codex_readonly_verified"] is True
    assert final.evidence["codex_mutations_detected"] == []


# ---------------------------------------------------------------------------
# Start-time refusals
# ---------------------------------------------------------------------------


def test_direct_branch_refuses_a_dirty_tree(config, store, repo):
    (repo / "uncommitted.txt").write_text("x\n")
    with pytest.raises(Exception, match="dirty working tree"):
        start(config, store, repo, delivery_mode="direct_branch")


def test_direct_branch_refuses_a_detached_head(config, store, repo):
    sha = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    git("checkout", "-q", "--detach", sha, cwd=repo)
    with pytest.raises(Exception, match="detached HEAD"):
        start(config, store, repo, delivery_mode="direct_branch")


def test_review_branch_tolerates_a_dirty_tree_and_ignores_those_changes(
    config, store, repo, fake_log_dir
):
    (repo / "unrelated.txt").write_text("not part of the run\n")
    record = start(config, store, repo)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    assert "unrelated.txt" not in final.owned_paths
    assert (repo / "unrelated.txt").is_file()


def test_a_repository_outside_the_allowlist_is_refused(config, store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    git("init", "-q", "-b", "main", cwd=outside)
    git("config", "user.email", "t@e.invalid", cwd=outside)
    git("config", "user.name", "T", cwd=outside)
    (outside / "a.txt").write_text("x\n")
    git("add", "a.txt", cwd=outside)
    git("commit", "-q", "-m", "init", cwd=outside)
    with pytest.raises(Exception, match="not below an allowed_repo_roots entry"):
        start(config, store, outside)


def test_a_second_run_on_the_same_repo_is_refused(config, store, repo):
    start(config, store, repo)
    with pytest.raises(Exception, match="already active"):
        start(config, store, repo)


def test_a_wrong_expected_base_ref_is_refused(config, store, repo):
    (repo / "b.txt").write_text("x\n")
    git("add", "b.txt", cwd=repo)
    git("commit", "-q", "-m", "second", cwd=repo)
    with pytest.raises(Exception, match="but HEAD is"):
        start(config, store, repo, expected_base_ref="HEAD~1")


def test_a_matching_expected_base_ref_is_accepted(config, store, repo):
    record = start(config, store, repo, expected_base_ref="HEAD")
    assert record.base_sha == git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def test_an_unresolvable_expected_base_ref_is_refused(config, store, repo):
    with pytest.raises(Exception, match="does not resolve"):
        start(config, store, repo, expected_base_ref="no-such-ref")


# ---------------------------------------------------------------------------
# Cancellation and locking
# ---------------------------------------------------------------------------


def test_cancelling_mid_run_stops_before_the_next_phase(config, store, repo, fake_log_dir):
    record = start(config, store, repo)
    store.request_cancel(record.run_id)
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.CANCELLED
    assert list(fake_log_dir.glob("claude-call-*.json")) == []


def test_a_held_repo_lock_fails_the_worker(config, store, repo, fake_log_dir):
    from agent_duet.git_guard import inspect_repo, repo_lock

    record = start(config, store, repo)
    with repo_lock(config.locks_dir, inspect_repo(repo).git_common_dir):
        run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.FAILED
    assert "already holds" in final.error


def test_direct_branch_works_in_place(config, store, repo, fake_log_dir):
    record = start(config, store, repo, delivery_mode="direct_branch")
    run_worker(config, store, record.run_id)
    final = store.get(record.run_id)
    assert final.phase is Phase.AWAITING_FINALIZE, final.error
    assert final.worktree == str(repo)
    assert final.branch == "main"
    assert (repo / "impl.py").is_file()
