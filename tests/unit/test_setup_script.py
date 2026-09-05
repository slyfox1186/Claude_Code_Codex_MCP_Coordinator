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
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "NO_COLOR": "1",
        }
    )
    return home, env


def _run_interactive_setup(
    *, cwd: Path, env: dict[str, str], answers: str
) -> subprocess.CompletedProcess[str]:
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            ["bash", str(PROJECT_ROOT / "setup.sh")],
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
