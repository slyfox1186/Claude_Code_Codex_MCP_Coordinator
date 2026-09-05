"""Black-box tests for the interactive setup script."""

from __future__ import annotations

import os
import pty
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_PYTHON = Path("/home/jman/miniconda3/bin/python")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fake_install_environment(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    python_bin = home / "miniconda3" / "bin"
    fake_bin.mkdir(parents=True)
    python_bin.mkdir(parents=True)

    _write_executable(
        python_bin / "python3",
        f"""#!/usr/bin/env bash
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
    exit 0
fi
exec {REAL_PYTHON} "$@"
""",
    )
    _write_executable(
        fake_bin / "claude",
        """#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "2.1.236 (Claude Code)"; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "codex",
        """#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "codex-cli 0.153.2"; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "agent-duet",
        f"""#!{python_bin / "python3"}
import sys

if sys.argv[1:] == ["--version"]:
    print("agent-duet 1.0.0")
""",
    )

    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CONDA_"):
            env.pop(name)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "NO_COLOR": "1",
        }
    )
    return home, env


def _write_fake_provider_clis(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "claude",
        '#!/bin/bash\n[[ "$1" == "--version" ]] && echo "2.1.236 (Claude Code)"\nexit 0\n',
    )
    _write_executable(
        fake_bin / "codex",
        '#!/bin/bash\n[[ "$1" == "--version" ]] && echo "codex-cli 0.153.2"\nexit 0\n',
    )


def _write_fake_python(path: Path, log: Path) -> None:
    _write_executable(
        path,
        f"""#!/bin/bash
printf '%s %s\\n' "$0" "$*" >> "{log}"
if [[ "$1" == "-m" && "$2" == "venv" ]]; then
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
    exit 0
fi
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
    if [[ " $* " == *" --editable "* ]]; then
        printf '#!%s\\nimport sys\\n' "$0" > "$(dirname "$0")/agent-duet"
        chmod 755 "$(dirname "$0")/agent-duet"
    fi
    exit 0
fi
exec {REAL_PYTHON} "$@"
""",
    )


def _python_setup_environment(
    tmp_path: Path, *, with_conda: bool
) -> tuple[Path, Path, Path, dict[str, str]]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    home.mkdir()
    python_log = tmp_path / "python.log"
    conda_log = tmp_path / "conda.log"
    template = tmp_path / "python-template"

    _write_fake_provider_clis(fake_bin)
    _write_fake_python(template, python_log)
    _write_fake_python(fake_bin / "python3", python_log)
    if with_conda:
        conda_base = home / "miniconda3"
        (conda_base / "bin").mkdir(parents=True)
        _write_executable(
            conda_base / "bin" / "conda",
            f"""#!/bin/bash
printf '%s\\n' "$*" >> "{conda_log}"
if [[ "$1" == "info" && "$2" == "--base" ]]; then
    echo "{conda_base}"
    exit 0
fi
if [[ "$1" == "create" || "$1" == "install" ]]; then
    mkdir -p "{conda_base}/envs/agent-duet/bin"
    cp "{template}" "{conda_base}/envs/agent-duet/bin/python"
    exit 0
fi
exit 1
""",
        )

    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CONDA_"):
            env.pop(name)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "NO_COLOR": "1",
        }
    )
    return home, python_log, conda_log, env


def _run_interactive_setup(
    *, cwd: Path, env: dict[str, str], answers: str, args: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            ["bash", str(PROJECT_ROOT / "setup.sh"), *args],
            cwd=cwd,
            env=env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        os.close(slave)
        slave = -1
        os.write(master, answers.encode())
        output, _ = process.communicate(timeout=30)
        return subprocess.CompletedProcess(process.args, process.returncode, output, "")
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)


@pytest.mark.parametrize("entered_path", ["absolute", "relative", "home"])
def test_declining_demo_prompts_for_and_registers_repository(
    tmp_path: Path, entered_path: str
) -> None:
    home, env = _fake_install_environment(tmp_path)
    project = (home if entered_path == "home" else tmp_path) / "projects" / "sample"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    answer = {
        "absolute": str(project),
        "relative": str(project.relative_to(tmp_path)),
        "home": "~/projects/sample",
    }[entered_path]

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers=f"n\n{answer}\n")

    assert result.returncode == 0, result.stdout
    assert "Project repository path" in result.stdout
    assert f"Registering {project}" in result.stdout
    assert f'path = "{project}"' in (home / ".config/agent-duet/config.toml").read_text()


def test_blank_repository_path_skips_registration(tmp_path: Path) -> None:
    home, env = _fake_install_environment(tmp_path)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    assert "No project registered" in result.stdout
    assert "./setup.sh add-repo /path/to/your/project" in result.stdout
    config = (home / ".config/agent-duet/config.toml").read_text()
    assert "\n[[repositories]]\n" not in config


def test_detected_conda_creates_only_named_agent_duet_environment(tmp_path: Path) -> None:
    home, _, conda_log, env = _python_setup_environment(tmp_path, with_conda=True)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    assert "dedicated Conda environment named agent-duet" in result.stdout
    calls = conda_log.read_text()
    assert "create --name agent-duet" in calls
    assert "-n base" not in calls
    assert "--name base" not in calls
    assert (home / "miniconda3/envs/agent-duet/bin/python").is_file()


def test_declining_conda_environment_creation_changes_nothing(tmp_path: Path) -> None:
    home, _, conda_log, env = _python_setup_environment(tmp_path, with_conda=True)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 2, result.stdout
    assert "installation not completed" in result.stdout
    calls = conda_log.read_text() if conda_log.exists() else ""
    assert "create" not in calls
    assert "install" not in calls
    assert not (home / "miniconda3/envs/agent-duet").exists()


def test_system_python_creates_private_environment_without_conda(tmp_path: Path) -> None:
    home, python_log, _, env = _python_setup_environment(tmp_path, with_conda=False)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    managed_python = home / ".local/share/agent-duet/venv/bin/python"
    assert "private Python environment" in result.stdout
    assert managed_python.is_file()
    calls = python_log.read_text().splitlines()
    system_python = tmp_path / "bin/python3"
    assert any(call == f"{system_python} -m venv {managed_python.parents[1]}" for call in calls)
    pip_calls = [call for call in calls if " -m pip " in call]
    assert pip_calls
    assert all(call.startswith(f"{managed_python} ") for call in pip_calls)
    assert "Miniconda" not in result.stdout


def test_existing_named_conda_environment_is_reused(tmp_path: Path) -> None:
    home, _, conda_log, env = _python_setup_environment(tmp_path, with_conda=True)
    env_python = home / "miniconda3/envs/agent-duet/bin/python"
    env_python.parent.mkdir(parents=True)
    _write_fake_python(env_python, tmp_path / "existing-python.log")

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    assert "using existing dedicated Conda environment" in result.stdout
    calls = conda_log.read_text()
    assert "create" not in calls
    assert "install" not in calls


def test_repair_targets_only_existing_named_conda_environment(tmp_path: Path) -> None:
    home, _, conda_log, env = _python_setup_environment(tmp_path, with_conda=True)
    (home / "miniconda3/envs/agent-duet").mkdir(parents=True)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    calls = conda_log.read_text()
    assert "install --name agent-duet" in calls
    assert "-n base" not in calls
    assert "--name base" not in calls


def test_old_system_python_does_not_trigger_conda_install(tmp_path: Path) -> None:
    _, _, _, env = _python_setup_environment(tmp_path, with_conda=False)
    _write_executable(tmp_path / "bin/python3", "#!/bin/bash\nexit 1\n")

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="")

    assert result.returncode == 1, result.stdout
    assert "Install Python 3.13 or newer" in result.stdout
    assert "Miniconda" not in result.stdout


def test_install_repair_does_not_create_or_modify_python_environment(tmp_path: Path) -> None:
    home, python_log, conda_log, env = _python_setup_environment(tmp_path, with_conda=True)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="", args=("install",))

    assert result.returncode == 1, result.stdout
    assert "run ./setup.sh first" in result.stdout
    calls = conda_log.read_text() if conda_log.exists() else ""
    assert "create" not in calls
    assert "install" not in calls
    python_calls = python_log.read_text().splitlines() if python_log.exists() else []
    pip_calls = [line for line in python_calls if " -m pip " in line]
    assert not pip_calls
    assert not (home / ".local/share/agent-duet/venv").exists()
