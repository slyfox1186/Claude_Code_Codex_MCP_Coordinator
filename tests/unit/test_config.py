#!/usr/bin/env python3
"""Configuration loading, the repository allowlist, and executable resolution."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from agent_duet.config import ConfigError, config_path, load_config

from helpers import FIXTURE_BIN


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body).strip() + "\n")
    return path


def base_body(work_root: Path, state: Path) -> str:
    return f"""
    allowed_repo_roots = ["{work_root}"]
    state_dir = "{state}"

    [claude]
    executable = "{FIXTURE_BIN / 'fake-claude'}"

    [codex]
    executable = "{FIXTURE_BIN / 'fake-codex'}"
    """


def test_valid_config_loads(tmp_path, work_root):
    path = write(tmp_path, base_body(work_root, tmp_path / "state"))
    config = load_config(path)
    assert config.claude_path.is_file()
    assert config.codex_path.is_file()
    assert config.source_path == path


def test_missing_config_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="configuration not found"):
        load_config(tmp_path / "absent.toml")


def test_unknown_key_is_rejected(tmp_path, work_root):
    path = write(
        tmp_path, base_body(work_root, tmp_path / "state") + '\nsurprise_key = "x"\n'
    )
    with pytest.raises(ConfigError, match="failed validation"):
        load_config(path)


def test_unknown_nested_key_is_rejected(tmp_path, work_root):
    body = base_body(work_root, tmp_path / "state").replace(
        f'executable = "{FIXTURE_BIN / "fake-codex"}"',
        f'executable = "{FIXTURE_BIN / "fake-codex"}"\nnope = 1',
    )
    with pytest.raises(ConfigError, match="failed validation"):
        load_config(write(tmp_path, body))


def test_malformed_toml_is_reported(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("this is not = = toml\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(path)


def test_missing_executable_is_rejected(tmp_path, work_root):
    body = base_body(work_root, tmp_path / "state").replace(
        str(FIXTURE_BIN / "fake-claude"), str(tmp_path / "nope")
    )
    with pytest.raises(ConfigError, match="not a regular file"):
        load_config(write(tmp_path, body))


def test_home_root_is_refused(tmp_path):
    body = f"""
    allowed_repo_roots = ["{Path.home()}"]
    state_dir = "{tmp_path / 'state'}"

    [claude]
    executable = "{FIXTURE_BIN / 'fake-claude'}"

    [codex]
    executable = "{FIXTURE_BIN / 'fake-codex'}"
    """
    with pytest.raises(ConfigError, match="too broad"):
        load_config(write(tmp_path, body))


def test_filesystem_root_is_refused(tmp_path):
    body = f"""
    allowed_repo_roots = ["/"]
    state_dir = "{tmp_path / 'state'}"

    [claude]
    executable = "{FIXTURE_BIN / 'fake-claude'}"

    [codex]
    executable = "{FIXTURE_BIN / 'fake-codex'}"
    """
    with pytest.raises(ConfigError, match="too broad"):
        load_config(write(tmp_path, body))


def test_relative_root_is_refused(tmp_path):
    body = f"""
    allowed_repo_roots = ["relative/path"]
    state_dir = "{tmp_path / 'state'}"

    [claude]
    executable = "{FIXTURE_BIN / 'fake-claude'}"

    [codex]
    executable = "{FIXTURE_BIN / 'fake-codex'}"
    """
    with pytest.raises(ConfigError, match="absolute"):
        load_config(write(tmp_path, body))


def test_root_allows_only_paths_strictly_below(tmp_path, work_root):
    config = load_config(write(tmp_path, base_body(work_root, tmp_path / "state")))
    assert config.root_allows(work_root / "project")
    assert config.root_allows(work_root / "nested" / "project")
    assert not config.root_allows(work_root), "the root itself is never a run target"
    assert not config.root_allows(Path("/etc"))
    assert not config.root_allows(tmp_path)


def test_duplicate_repository_entries_are_rejected(tmp_path, work_root, repo):
    body = base_body(work_root, tmp_path / "state") + f"""
    [[repositories]]
    path = "{repo}"

    [[repositories]]
    path = "{repo}"
    """
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, body))


def test_unknown_deployment_profile_is_rejected(tmp_path, work_root, repo):
    body = base_body(work_root, tmp_path / "state") + f"""
    [[repositories]]
    path = "{repo}"
    deployment_profile = "does-not-exist"
    """
    with pytest.raises(ConfigError, match="unknown deployment profile"):
        load_config(write(tmp_path, body))


def test_empty_validation_vector_is_rejected(tmp_path, work_root, repo):
    body = base_body(work_root, tmp_path / "state") + f"""
    [[repositories]]
    path = "{repo}"
    validation_commands = [[]]
    """
    with pytest.raises(ConfigError, match="failed validation"):
        load_config(write(tmp_path, body))


def test_validation_commands_must_be_vectors_not_strings(tmp_path, work_root, repo):
    body = base_body(work_root, tmp_path / "state") + f"""
    [[repositories]]
    path = "{repo}"
    validation_commands = ["pytest -q && rm -rf /"]
    """
    with pytest.raises(ConfigError, match="failed validation"):
        load_config(write(tmp_path, body))


def test_repository_lookup_is_by_canonical_path(tmp_path, work_root, repo):
    body = base_body(work_root, tmp_path / "state") + f"""
    [[repositories]]
    path = "{repo}"
    validation_commands = [["true"]]
    """
    config = load_config(write(tmp_path, body))
    assert config.repository_for(repo) is not None
    assert config.repository_for(work_root / "other") is None


def test_config_path_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_DUET_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_path() == tmp_path / "agent-duet" / "config.toml"


def test_config_path_honours_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DUET_CONFIG", str(tmp_path / "custom.toml"))
    assert config_path() == tmp_path / "custom.toml"


def test_full_access_defaults_are_the_shipped_posture(tmp_path, work_root):
    config = load_config(write(tmp_path, base_body(work_root, tmp_path / "state")))
    assert config.claude.dangerously_skip_permissions is True
    assert config.codex.sandbox_mode == "bypass"
    assert config.child_env_mode == "inherit"
    assert config.claude.allowed_tools == [], "empty means the full built-in toolset"
    assert "mcp__*" in config.claude.disallowed_tools


def test_state_dirs_are_private(config):
    from agent_duet.config import ensure_state_dirs

    ensure_state_dirs(config)
    for directory in (config.state_path, config.runs_dir, config.worktrees_dir, config.locks_dir):
        assert oct(directory.stat().st_mode & 0o777) == "0o700"
