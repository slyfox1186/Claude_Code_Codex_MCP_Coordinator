#!/usr/bin/env python3
"""Shared fixtures: a throwaway repository, a config wired to the fake CLIs, and helpers."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from agent_duet.config import Config, ensure_state_dirs, load_config
from agent_duet.state import StateStore

from helpers import FIXTURE_BIN, git


@pytest.fixture
def work_root(tmp_path: Path) -> Path:
    """An allowlisted root that is not tmp_path itself, so the root-guard is exercised."""
    root = tmp_path / "work"
    root.mkdir()
    return root


@pytest.fixture
def repo(work_root: Path) -> Path:
    """A tiny git repository with one commit and an 'origin' remote."""
    path = work_root / "example"
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "test@example.invalid", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    git("config", "commit.gpgsign", "false", cwd=path)
    (path / "README.md").write_text("# example\n")
    git("add", "README.md", cwd=path)
    git("commit", "-q", "-m", "initial", cwd=path)
    git("remote", "add", "origin", "git@github.com:example/example.git", cwd=path)
    return path


@pytest.fixture
def bare_remote(work_root: Path, repo: Path) -> Path:
    """A bare repository standing in for a real remote, wired up as 'origin'."""
    path = work_root / "example-remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True
    )
    git("remote", "set-url", "origin", str(path), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    return path


@pytest.fixture
def config_file(tmp_path: Path, work_root: Path, repo: Path) -> Path:
    """A valid config.toml pointing at the fake CLIs and a state dir under tmp_path."""
    state = tmp_path / "state"
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            allowed_repo_roots = ["{work_root}"]
            state_dir = "{state}"
            wait_max_seconds = 5
            phase_timeout_seconds = 120
            child_env_mode = "inherit"

            [claude]
            executable = "{FIXTURE_BIN / 'fake-claude'}"
            dangerously_skip_permissions = true
            timeout_seconds = 120

            [codex]
            executable = "{FIXTURE_BIN / 'fake-codex'}"
            sandbox_mode = "bypass"
            write_policy = "warn"
            timeout_seconds = 120

            [git]
            branch_prefix = "agent-duet/"
            allowed_remote_names = ["origin"]

            [[repositories]]
            path = "{repo}"
            validation_commands = []
            """
        ).strip()
        + "\n"
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def config(config_file: Path) -> Config:
    loaded = load_config(config_file)
    ensure_state_dirs(loaded)
    return loaded


@pytest.fixture
def store(config: Config) -> StateStore:
    return StateStore(config.db_path)


@pytest.fixture
def fake_log_dir(tmp_path: Path) -> Path:
    """Where the fake CLIs record their argv and environment."""
    path = tmp_path / "fake-logs"
    path.mkdir()
    os.environ["FAKE_LOG_DIR"] = str(path)
    yield path
    os.environ.pop("FAKE_LOG_DIR", None)


@pytest.fixture(autouse=True)
def _clean_fake_env():
    """Keep behaviour switches from leaking between tests."""
    for name in ("FAKE_CLAUDE_BEHAVIOR", "FAKE_CODEX_BEHAVIOR", "AGENT_DUET_CHILD"):
        os.environ.pop(name, None)
    yield
    for name in ("FAKE_CLAUDE_BEHAVIOR", "FAKE_CODEX_BEHAVIOR", "AGENT_DUET_CHILD"):
        os.environ.pop(name, None)
