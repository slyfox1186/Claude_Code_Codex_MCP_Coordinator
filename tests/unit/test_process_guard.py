#!/usr/bin/env python3
"""Recursion guard, child environment construction, and PID-reuse-safe signalling."""

from __future__ import annotations

import os
import sys
import time

import pytest
from agent_duet.process_guard import (
    CHILD_ENV_VAR,
    MINIMAL_ENV_ALLOWLIST,
    RecursionError_,
    child_env,
    is_child_process,
    process_alive,
    process_start_ticks,
    refuse_if_child,
    self_worker_argv,
    spawn_detached_worker,
    terminate_process_group,
)
from agent_duet.redact import secret_env_names


def test_not_a_child_by_default(monkeypatch):
    monkeypatch.delenv(CHILD_ENV_VAR, raising=False)
    assert is_child_process() is False
    refuse_if_child("anything")  # must not raise


def test_child_marker_blocks_mutating_operations(monkeypatch):
    monkeypatch.setenv(CHILD_ENV_VAR, "1")
    assert is_child_process() is True
    for operation in ("duet_start", "duet_cancel", "duet_finalize", "serve"):
        with pytest.raises(RecursionError_, match=operation):
            refuse_if_child(operation)


def test_child_marker_must_be_exactly_one(monkeypatch):
    monkeypatch.setenv(CHILD_ENV_VAR, "0")
    assert is_child_process() is False


def test_child_env_always_sets_the_marker(monkeypatch):
    monkeypatch.delenv(CHILD_ENV_VAR, raising=False)
    assert child_env()[CHILD_ENV_VAR] == "1"
    assert child_env(mode="minimal")[CHILD_ENV_VAR] == "1"


def test_inherit_mode_passes_the_operators_environment(monkeypatch):
    monkeypatch.setenv("SOME_UNUSUAL_VARIABLE", "value")
    assert child_env(mode="inherit")["SOME_UNUSUAL_VARIABLE"] == "value"


def test_minimal_mode_drops_credential_shaped_variables(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "A" * 36)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("HOME", "/home/tester")
    env = child_env(mode="minimal")
    assert secret_env_names(env) == []
    assert env["HOME"] == "/home/tester"


def test_minimal_mode_keeps_what_the_clis_need(monkeypatch):
    for name in ("HOME", "PATH", "SSH_AUTH_SOCK", "CODEX_HOME"):
        monkeypatch.setenv(name, f"value-for-{name}")
    env = child_env(mode="minimal")
    for name in ("HOME", "PATH", "SSH_AUTH_SOCK", "CODEX_HOME"):
        assert env[name] == f"value-for-{name}"


def test_minimal_allowlist_contains_no_secret_shaped_names():
    assert secret_env_names(dict.fromkeys(MINIMAL_ENV_ALLOWLIST, "x")) == []


def test_child_env_never_leaks_our_config_pointer(monkeypatch):
    monkeypatch.setenv("AGENT_DUET_CONFIG", "/etc/agent-duet.toml")
    assert "AGENT_DUET_CONFIG" not in child_env()


def test_start_ticks_are_readable_and_stable():
    ticks = process_start_ticks(os.getpid())
    assert ticks is not None and ticks.isdigit()
    assert process_start_ticks(os.getpid()) == ticks


def test_start_ticks_for_a_missing_pid():
    assert process_start_ticks(4_000_000) is None


def test_process_alive_agrees_with_reality():
    assert process_alive(os.getpid(), process_start_ticks(os.getpid())) is True
    assert process_alive(4_000_000, None) is False
    assert process_alive(-1, None) is False


def test_process_alive_rejects_a_mismatched_start_time():
    """A recycled PID must not be mistaken for our process."""
    assert process_alive(os.getpid(), "99999999999999") is False


def test_spawn_detached_worker_creates_its_own_session(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text("import time; time.sleep(30)\n")
    worker = spawn_detached_worker(
        [sys.executable, str(script)], cwd=tmp_path, log_dir=tmp_path / "logs"
    )
    try:
        assert worker.pid > 0
        assert worker.pgid == worker.pid, "the worker leads its own process group"
        assert worker.pgid != os.getpgid(os.getpid())
        assert worker.stdout_log.is_file()
        assert oct(worker.stdout_log.stat().st_mode & 0o777) == "0o600"
    finally:
        terminate_process_group(worker.pgid, pid=worker.pid, start_ticks=worker.start_ticks)


def test_spawned_worker_output_lands_in_the_log(tmp_path):
    script = tmp_path / "talker.py"
    script.write_text("print('hello from the worker')\n")
    worker = spawn_detached_worker(
        [sys.executable, str(script)], cwd=tmp_path, log_dir=tmp_path / "logs"
    )
    for _ in range(50):
        if worker.stdout_log.read_text().strip():
            break
        time.sleep(0.1)
    assert "hello from the worker" in worker.stdout_log.read_text()


def test_terminate_reaps_the_whole_group(tmp_path):
    """A grandchild in the same group must die with its parent."""
    marker = tmp_path / "grandchild.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
        "time.sleep(300)\n"
    )
    worker = spawn_detached_worker(
        [sys.executable, str(script)], cwd=tmp_path, log_dir=tmp_path / "logs"
    )
    for _ in range(100):
        if marker.is_file():
            break
        time.sleep(0.1)
    grandchild = int(marker.read_text())
    outcome = terminate_process_group(
        worker.pgid, pid=worker.pid, start_ticks=worker.start_ticks, grace_seconds=5
    )
    assert "terminat" in outcome or "killed" in outcome
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(grandchild, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild survived the group termination")


def test_terminate_kills_a_process_that_ignores_sigterm(tmp_path):
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(300)\n"
    )
    worker = spawn_detached_worker(
        [sys.executable, str(script)], cwd=tmp_path, log_dir=tmp_path / "logs"
    )
    time.sleep(0.5)
    outcome = terminate_process_group(
        worker.pgid, pid=worker.pid, start_ticks=worker.start_ticks, grace_seconds=2
    )
    assert outcome == "killed after grace period"
    assert not process_alive(worker.pid, worker.start_ticks)


def test_terminate_refuses_an_implausible_group():
    assert "refused" in terminate_process_group(1)
    assert "refused" in terminate_process_group(0)


def test_terminate_is_safe_when_the_pid_was_reused():
    assert "already exited" in terminate_process_group(
        os.getpgid(os.getpid()), pid=os.getpid(), start_ticks="99999999999999"
    )


def test_terminate_is_idempotent(tmp_path):
    script = tmp_path / "quick.py"
    script.write_text("pass\n")
    worker = spawn_detached_worker(
        [sys.executable, str(script)], cwd=tmp_path, log_dir=tmp_path / "logs"
    )
    time.sleep(0.5)
    terminate_process_group(worker.pgid, pid=worker.pid, start_ticks=worker.start_ticks)
    terminate_process_group(worker.pgid, pid=worker.pid, start_ticks=worker.start_ticks)


def test_worker_argv_reexecutes_this_installation():
    argv = self_worker_argv("abc-123")
    assert argv[0] == sys.executable
    assert argv[1:] == ["-m", "agent_duet", "worker", "--run-id", "abc-123"]
