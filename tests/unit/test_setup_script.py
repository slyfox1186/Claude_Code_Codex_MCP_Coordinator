"""Black-box tests for the interactive setup script."""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_PYTHON = Path(sys.executable)
POLL_PERMISSIONS = (
    "mcp__agent_duet__duet_status",
    "mcp__agent_duet__duet_wait",
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _write_claude_poll_permissions(home: Path, *other_allow: str) -> Path:
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {"permissions": {"allow": [*other_allow, *POLL_PERMISSIONS]}},
            indent=2,
        )
        + "\n"
    )
    return settings


def _fake_install_environment(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    python_bin = home / ".local/share/agent-duet/venv/bin"
    fake_bin.mkdir(parents=True)
    python_bin.mkdir(parents=True)

    _write_executable(
        python_bin / "python",
        f"""#!/usr/bin/env bash
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
    exit 0
fi
exec "{REAL_PYTHON}" "$@"
""",
    )
    _write_executable(
        fake_bin / "claude",
        """#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "2.1.236 (Claude Code)"; fi
if [[ "$*" == "mcp get agent_duet" ]]; then echo "Connected"; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "codex",
        """#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "codex-cli 0.153.2"; fi
if [[ "$*" == "mcp get agent_duet" ]]; then echo "duet_finalize"; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "agent-duet",
        f"""#!{python_bin / "python"}
import sys

if sys.argv[1:] == ["--version"]:
    print("agent-duet 1.0.0")
""",
    )

    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CONDA_"):
            env.pop(name)
    for name in ("CODEX_INSTALL_DIR", "DUET_PYTHON"):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "NO_COLOR": "1",
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
        }
    )
    return home, env


def _write_fake_provider_cli(
    path: Path, *, provider: str, log: Path, authenticated: bool = True
) -> None:
    version = "2.1.236 (Claude Code)" if provider == "claude" else "codex-cli 0.153.2"
    auth_command = "auth status" if provider == "claude" else "login status"
    _write_executable(
        path,
        f"""#!/bin/bash
printf '{provider} %s\\n' "$*" >> "{log}"
if [[ "$*" == "--version" ]]; then
    echo "{version}"
    exit 0
fi
if [[ "$*" == "{auth_command}" ]]; then
    exit {0 if authenticated else 1}
fi
if [[ "$*" == "mcp get agent_duet" ]]; then
    [[ "{provider}" == "claude" ]] && echo "Connected" || echo "duet_finalize"
    exit 0
fi
exit 0
""",
    )


def _write_fake_provider_clis(fake_bin: Path, log: Path | None = None) -> None:
    provider_log = log or fake_bin / "providers.log"
    _write_fake_provider_cli(fake_bin / "claude", provider="claude", log=provider_log)
    _write_fake_provider_cli(fake_bin / "codex", provider="codex", log=provider_log)


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
exec "{REAL_PYTHON}" "$@"
""",
    )


def _python_setup_environment(
    tmp_path: Path, *, with_conda: bool, conda_environment_root: Path | None = None
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
        conda_environment = (conda_environment_root or conda_base / "envs") / "agent-duet"
        (conda_base / "bin").mkdir(parents=True)
        _write_executable(
            conda_base / "bin" / "conda",
            f"""#!/bin/bash
printf '%s\\n' "$*" >> "{conda_log}"
if [[ "$1" == "info" && "$2" == "--base" ]]; then
    echo "{conda_base}"
    exit 0
fi
if [[ "$1" == "run" && "$2" == "--name" && "$3" == "agent-duet" ]]; then
    if [[ -x "{conda_environment}/bin/python" ]]; then
        echo "{conda_environment}/bin/python"
        exit 0
    fi
    exit 1
fi
if [[ "$1" == "create" || "$1" == "install" ]]; then
    target="{conda_environment}"
    previous=""
    for argument in "$@"; do
        if [[ "$previous" == "--prefix" ]]; then target="$argument"; break; fi
        previous="$argument"
    done
    mkdir -p "$target/bin"
    cp "{template}" "$target/bin/python"
    exit 0
fi
exit 1
""",
        )

    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CONDA_"):
            env.pop(name)
    for name in ("CODEX_INSTALL_DIR", "DUET_PYTHON"):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "NO_COLOR": "1",
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
        }
    )
    return home, python_log, conda_log, env


def _guided_setup_environment(
    tmp_path: Path,
    *,
    claude_present: bool = True,
    codex_present: bool = True,
    claude_authenticated: bool = True,
    codex_authenticated: bool = True,
) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    managed_bin = home / ".local/share/agent-duet/venv/bin"
    user_bin = home / ".local/bin"
    for directory in (home, fake_bin, managed_bin, user_bin):
        directory.mkdir(parents=True, exist_ok=True)

    python_log = tmp_path / "python.log"
    provider_log = tmp_path / "providers.log"
    download_log = tmp_path / "downloads.log"
    _write_fake_python(managed_bin / "python", python_log)
    _write_fake_python(fake_bin / "python3", python_log)

    templates = tmp_path / "provider-templates"
    templates.mkdir()
    claude_template = templates / "claude"
    codex_template = templates / "codex"
    _write_fake_provider_cli(
        claude_template,
        provider="claude",
        log=provider_log,
        authenticated=claude_authenticated,
    )
    _write_fake_provider_cli(
        codex_template,
        provider="codex",
        log=provider_log,
        authenticated=codex_authenticated,
    )
    if claude_present:
        (fake_bin / "claude").symlink_to(claude_template)
    if codex_present:
        (fake_bin / "codex").symlink_to(codex_template)

    claude_installer = tmp_path / "claude-installer.sh"
    codex_installer = tmp_path / "codex-installer.sh"
    _write_executable(
        claude_installer,
        '#!/bin/bash\ncp "$FAKE_CLAUDE_BINARY" "$HOME/.local/bin/claude"\n'
        'chmod 755 "$HOME/.local/bin/claude"\n',
    )
    _write_executable(
        codex_installer,
        '#!/bin/bash\ncp "$FAKE_CODEX_BINARY" "$HOME/.local/bin/codex"\n'
        'chmod 755 "$HOME/.local/bin/codex"\n',
    )
    _write_executable(
        fake_bin / "curl",
        f"""#!/bin/bash
output=""
url=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o) output="$2"; shift 2 ;;
        http*) url="$1"; shift ;;
        *) shift ;;
    esac
done
printf '%s\\n' "$url" >> "{download_log}"
if [[ "$url" == *"claude.ai"* ]]; then
    cp "$FAKE_CLAUDE_INSTALLER" "$output"
else
    cp "$FAKE_CODEX_INSTALLER" "$output"
fi
""",
    )

    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("CONDA_"):
            env.pop(name)
    for name in ("CODEX_INSTALL_DIR", "DUET_PYTHON"):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "NO_COLOR": "1",
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "FAKE_CLAUDE_BINARY": str(claude_template),
            "FAKE_CODEX_BINARY": str(codex_template),
            "FAKE_CLAUDE_INSTALLER": str(claude_installer),
            "FAKE_CODEX_INSTALLER": str(codex_installer),
            "SHELL": "/bin/bash",
        }
    )
    return home, python_log, provider_log, download_log, env


def _run_interactive_setup(
    *, cwd: Path, env: dict[str, str], answers: str, args: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            ["/bin/bash", str(PROJECT_ROOT / "setup.sh"), *args],
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
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    answer = {
        "absolute": str(project),
        "relative": str(project.relative_to(tmp_path)),
        "home": "~/projects/sample",
    }[entered_path]

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers=f"n\n{answer}\n")

    assert result.returncode == 0, result.stdout
    assert "Project directory path" in result.stdout
    assert "Project repository path" not in result.stdout
    assert f"Registering {project}" in result.stdout
    assert f'path = "{project}"' in (home / ".config/agent-duet/config.toml").read_text()


def test_guided_setup_initializes_plain_directory_without_an_extra_prompt(
    tmp_path: Path,
) -> None:
    home, env = _fake_install_environment(tmp_path)
    project = tmp_path / "plain-folder"
    project.mkdir()
    (project / ".gitignore").write_text(".env\n")
    (project / ".env").write_text("LOCAL_ONLY=ignored\n")
    (project / "app.py").write_text("print('hello')\n")

    result = _run_interactive_setup(
        cwd=tmp_path,
        env=env,
        answers=f"n\n{project}/ \n",
    )

    assert result.returncode == 0, result.stdout
    assert "Create the local Git baseline now?" not in result.stdout
    assert "created a local Git baseline" in result.stdout
    assert "Nothing will be uploaded" in result.stdout
    assert (project / ".git").is_dir()
    tracked = subprocess.run(
        ["git", "-C", str(project), "ls-files"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()
    assert tracked == [".gitignore", "app.py"]
    assert f"Registering {project}" in result.stdout
    assert f'path = "{project}"' in (home / ".config/agent-duet/config.toml").read_text()


@pytest.mark.parametrize("option", ["-d", "--directory", "--directory="])
def test_directory_option_registers_without_project_or_demo_prompt(
    tmp_path: Path, option: str
) -> None:
    home, env = _fake_install_environment(tmp_path)
    project = tmp_path / "projects" / "sample"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )

    directory_args = (
        (f"--directory={project}/",) if option.endswith("=") else (option, f"{project}/")
    )
    result = _run_interactive_setup(
        cwd=tmp_path,
        env=env,
        answers="",
        args=directory_args,
    )

    assert result.returncode == 0, result.stdout
    assert "Try it now on a throwaway project?" not in result.stdout
    assert "Project directory path" not in result.stdout
    assert f"Registering {project}" in result.stdout
    assert f'path = "{project}"' in (home / ".config/agent-duet/config.toml").read_text()


def test_directory_option_prepares_plain_directory_without_prompting(
    tmp_path: Path,
) -> None:
    home, env = _fake_install_environment(tmp_path)
    project = tmp_path / "plain-folder"
    project.mkdir()
    source = project / "app.py"
    source.write_text("print('hello')\n")

    result = _run_interactive_setup(
        cwd=tmp_path,
        env=env,
        answers="",
        args=("-d", str(project)),
    )

    assert result.returncode == 0, result.stdout
    assert "Create the local Git baseline now?" not in result.stdout
    assert "created a local Git baseline" in result.stdout
    assert (project / ".git").is_dir()
    assert source.read_text() == "print('hello')\n"
    assert f'path = "{project}"' in (home / ".config/agent-duet/config.toml").read_text()


def test_blank_repository_path_skips_registration(tmp_path: Path) -> None:
    home, env = _fake_install_environment(tmp_path)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    assert "No project registered" in result.stdout
    assert "./setup.sh add-repo /path/to/your/project" in result.stdout
    config = (home / ".config/agent-duet/config.toml").read_text()
    assert "max_parallel_global = 2" in config
    assert "\n[[repositories]]\n" not in config


def test_python_project_installs_declared_dependencies_in_isolated_environment(
    tmp_path: Path,
) -> None:
    home, python_log, _, _, env = _guided_setup_environment(tmp_path)
    project = tmp_path / "projects" / "sample"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    (project / "test_sample.py").write_text("def test_sample(): assert True\n")
    (project / "constraints.txt").write_text("example-runtime==1.2.3\n")
    (project / "requirements.txt").write_text("example-runtime\n")
    (project / "requirements-dev.txt").write_text("pytest==9.1.1\n")

    result = _run_interactive_setup(
        cwd=tmp_path,
        env=env,
        answers=f"n\n{project}\ny\n",
    )

    assert result.returncode == 0, result.stdout
    assert "isolated validation environment" in result.stdout
    project_env_root = home / ".local/share/agent-duet/project-envs"
    validation_pythons = list(project_env_root.glob("*/bin/python"))
    assert len(validation_pythons) == 1
    validation_python = validation_pythons[0]
    assert validation_python != home / ".local/share/agent-duet/venv/bin/python"
    pip_calls = [line for line in python_log.read_text().splitlines() if " -m pip " in line]
    project_pip_calls = [line for line in pip_calls if line.startswith(f"{validation_python} ")]
    assert len(project_pip_calls) == 1
    assert f"-c {project / 'constraints.txt'}" in project_pip_calls[0]
    assert f"-r {project / 'requirements.txt'}" in project_pip_calls[0]
    assert f"-r {project / 'requirements-dev.txt'}" in project_pip_calls[0]
    config = (home / ".config/agent-duet/config.toml").read_text()
    assert f'["{validation_python}", "-m", "pytest", "-q"]' in config
    assert (
        f'["{home / ".local/share/agent-duet/venv/bin/python"}, "-m", "pytest", "-q"]'
        not in config
    )


def test_declining_python_project_dependency_install_does_not_write_false_validation(
    tmp_path: Path,
) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)
    project = tmp_path / "projects" / "sample"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    (project / "test_sample.py").write_text("def test_sample(): assert True\n")
    (project / "requirements-dev.txt").write_text("pytest==9.1.1\n")

    result = _run_interactive_setup(
        cwd=tmp_path,
        env=env,
        answers=f"n\n{project}\nn\n",
    )

    assert result.returncode == 0, result.stdout
    assert "will be registered without an automatic validation command" in result.stdout
    config = (home / ".config/agent-duet/config.toml").read_text()
    assert "validation_commands = []" in config
    assert not (home / ".local/share/agent-duet/project-envs").exists()


def test_python_project_reuses_local_pytest_environment(tmp_path: Path) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)
    project = tmp_path / "projects" / "sample"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    (project / "test_sample.py").write_text("def test_sample(): assert True\n")
    local_python = project / ".venv/bin/python"
    local_python.parent.mkdir(parents=True)
    _write_executable(
        local_python,
        f"#!/bin/bash\nexec \"{REAL_PYTHON}\" \"$@\"\n",
    )

    result = _run_interactive_setup(
        cwd=tmp_path,
        env=env,
        answers=f"n\n{project}\n",
    )

    assert result.returncode == 0, result.stdout
    assert f"using existing project environment: {local_python}" in result.stdout
    config = (home / ".config/agent-duet/config.toml").read_text()
    assert f'["{local_python}", "-m", "pytest", "-q"]' in config
    assert not (home / ".local/share/agent-duet/project-envs").exists()


def test_python_project_uses_separate_conda_prefix_environment(tmp_path: Path) -> None:
    home, _, conda_log, env = _python_setup_environment(tmp_path, with_conda=True)
    agent_python = home / "miniconda3/envs/agent-duet/bin/python"
    agent_python.parent.mkdir(parents=True)
    _write_fake_python(agent_python, tmp_path / "agent-python.log")
    initial = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")
    assert initial.returncode == 0, initial.stdout
    project = tmp_path / "projects" / "sample"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    (project / "test_sample.py").write_text("def test_sample(): assert True\n")
    (project / "requirements-dev.txt").write_text("pytest==9.1.1\n")

    result = _run_interactive_setup(
        cwd=tmp_path,
        env=env,
        answers="y\n",
        args=("add-repo", str(project)),
    )

    assert result.returncode == 0, result.stdout
    project_env_root = home / ".local/share/agent-duet/project-envs"
    validation_pythons = list(project_env_root.glob("*/bin/python"))
    assert len(validation_pythons) == 1
    assert validation_pythons[0] != agent_python
    conda_calls = conda_log.read_text()
    assert f"create --prefix {validation_pythons[0].parents[1]} --yes python=3.13 pip" in conda_calls
    assert "--name base" not in conda_calls
    config = (home / ".config/agent-duet/config.toml").read_text()
    assert f'["{validation_pythons[0]}", "-m", "pytest", "-q"]' in config


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


def test_existing_private_environment_does_not_require_system_python(tmp_path: Path) -> None:
    _, _, _, _, env = _guided_setup_environment(tmp_path)
    _write_executable(tmp_path / "bin/python3", "#!/bin/bash\nexit 1\n")

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    assert "using existing private Python environment" in result.stdout


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


def test_named_conda_environment_uses_condas_configured_location(tmp_path: Path) -> None:
    custom_root = tmp_path / "custom-conda-envs"
    _, python_log, conda_log, env = _python_setup_environment(
        tmp_path, with_conda=True, conda_environment_root=custom_root
    )

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    custom_python = custom_root / "agent-duet/bin/python"
    assert custom_python.is_file()
    assert "create --name agent-duet" in conda_log.read_text()
    pip_calls = [line for line in python_log.read_text().splitlines() if " -m pip " in line]
    assert pip_calls
    assert all(call.startswith(f"{custom_python} ") for call in pip_calls)


def test_repair_targets_only_existing_named_conda_environment(tmp_path: Path) -> None:
    home, _, conda_log, env = _python_setup_environment(tmp_path, with_conda=True)
    incompatible_python = home / "miniconda3/envs/agent-duet/bin/python"
    incompatible_python.parent.mkdir(parents=True)
    _write_executable(incompatible_python, "#!/bin/bash\nexit 1\n")

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    calls = conda_log.read_text()
    assert "install --name agent-duet" in calls
    assert "-n base" not in calls
    assert "--name base" not in calls


def test_detected_conda_does_not_install_into_duet_python_override(tmp_path: Path) -> None:
    _, _, conda_log, env = _python_setup_environment(tmp_path, with_conda=True)
    override = tmp_path / "unrelated-env/bin/python"
    override_log = tmp_path / "override.log"
    override.parent.mkdir(parents=True)
    _write_fake_python(override, override_log)
    env["DUET_PYTHON"] = str(override)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    assert "create --name agent-duet" in conda_log.read_text()
    override_calls = override_log.read_text().splitlines() if override_log.exists() else []
    assert not [call for call in override_calls if " -m pip " in call]


def test_no_conda_ignores_duet_python_override_and_creates_private_env(tmp_path: Path) -> None:
    home, python_log, _, env = _python_setup_environment(tmp_path, with_conda=False)
    override = tmp_path / "unrelated-env/bin/python"
    override_log = tmp_path / "override.log"
    override.parent.mkdir(parents=True)
    _write_fake_python(override, override_log)
    env["DUET_PYTHON"] = str(override)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    managed_python = home / ".local/share/agent-duet/venv/bin/python"
    assert managed_python.is_file()
    assert any(" -m venv " in call for call in python_log.read_text().splitlines())
    override_calls = override_log.read_text().splitlines() if override_log.exists() else []
    assert not [call for call in override_calls if " -m pip " in call]


def test_existing_system_python_launcher_is_not_reused(tmp_path: Path) -> None:
    home, python_log, _, env = _python_setup_environment(tmp_path, with_conda=False)
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "agent-duet",
        f"#!{fake_bin / 'python3'}\nimport sys\n",
    )

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    assert (home / ".local/share/agent-duet/venv/bin/python").is_file()
    assert any(" -m venv " in call for call in python_log.read_text().splitlines())


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


@pytest.mark.parametrize(
    ("missing_provider", "url"),
    [
        ("claude", "https://claude.ai/install.sh"),
        ("codex", "https://chatgpt.com/codex/install.sh"),
    ],
)
def test_missing_provider_requires_consent_before_official_install(
    tmp_path: Path, missing_provider: str, url: str
) -> None:
    home, _, _, download_log, env = _guided_setup_environment(
        tmp_path,
        claude_present=missing_provider != "claude",
        codex_present=missing_provider != "codex",
    )

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n")

    assert result.returncode == 2, result.stdout
    assert url in result.stdout
    assert "installation not completed" in result.stdout
    assert not download_log.exists()
    assert not (home / ".local/bin" / missing_provider).exists()
    if missing_provider == "claude":
        assert f"{home}/.local/bin/claude" in result.stdout
        assert f"{home}/.claude" in result.stdout
    else:
        assert f"{home}/.local/bin/codex" in result.stdout
        assert f"{home}/.codex/packages/standalone" in result.stdout
        assert f"{home}/.bashrc" in result.stdout


@pytest.mark.parametrize(
    ("missing_provider", "url"),
    [
        ("claude", "https://claude.ai/install.sh"),
        ("codex", "https://chatgpt.com/codex/install.sh"),
    ],
)
def test_consent_installs_missing_provider_from_official_source(
    tmp_path: Path, missing_provider: str, url: str
) -> None:
    home, _, _, download_log, env = _guided_setup_environment(
        tmp_path,
        claude_present=missing_provider != "claude",
        codex_present=missing_provider != "codex",
    )
    installer_temp = tmp_path / "installer-temp"
    installer_temp.mkdir()
    env["TMPDIR"] = str(installer_temp)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    assert download_log.read_text().splitlines() == [url]
    assert (home / ".local/bin" / missing_provider).is_file()
    assert list(installer_temp.iterdir()) == []


def test_failed_provider_installer_cleans_temporary_download(tmp_path: Path) -> None:
    _, _, _, _, env = _guided_setup_environment(tmp_path, claude_present=False)
    installer_temp = tmp_path / "installer-temp"
    installer_temp.mkdir()
    env["TMPDIR"] = str(installer_temp)
    _write_executable(Path(env["FAKE_CLAUDE_INSTALLER"]), "#!/bin/bash\nexit 9\n")

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\n")

    assert result.returncode == 1, result.stdout
    assert "official installer failed" in result.stdout
    assert list(installer_temp.iterdir()) == []


def test_ssh_codex_login_uses_device_auth_after_consent(tmp_path: Path) -> None:
    _, _, provider_log, _, env = _guided_setup_environment(
        tmp_path, codex_authenticated=False
    )
    env["SSH_CONNECTION"] = "client server"

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    assert "codex login --device-auth" in provider_log.read_text().splitlines()


def test_claude_login_is_offered_and_run_only_after_consent(tmp_path: Path) -> None:
    _, _, provider_log, _, env = _guided_setup_environment(
        tmp_path, claude_authenticated=False
    )

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\nn\n\n")

    assert result.returncode == 0, result.stdout
    assert "claude auth login" in provider_log.read_text().splitlines()


def test_declining_provider_logins_only_prints_later_commands(tmp_path: Path) -> None:
    _, _, provider_log, _, env = _guided_setup_environment(
        tmp_path, claude_authenticated=False, codex_authenticated=False
    )
    env["SSH_CONNECTION"] = "client server"

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\nn\nn\n\n")

    assert result.returncode == 0, result.stdout
    assert "Later, run: claude auth login" in result.stdout
    assert "Later, run: codex login --device-auth" in result.stdout
    provider_calls = provider_log.read_text().splitlines()
    assert "claude auth login" not in provider_calls
    assert "codex login --device-auth" not in provider_calls


def test_yes_flag_is_explicit_consent_for_required_cli_install(tmp_path: Path) -> None:
    home, _, _, download_log, env = _guided_setup_environment(
        tmp_path, claude_present=False
    )

    existing_demo = home / "duet-demo"
    existing_demo.mkdir()
    sentinel = existing_demo / "DO_NOT_DELETE"
    sentinel.write_text("owned by user")

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "setup.sh"), "--yes"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert download_log.read_text().splitlines() == ["https://claude.ai/install.sh"]
    assert (home / ".local/bin/claude").is_file()
    assert sentinel.read_text() == "owned by user"


def test_yes_flag_does_not_consent_to_interactive_provider_logins(tmp_path: Path) -> None:
    _, _, provider_log, _, env = _guided_setup_environment(
        tmp_path, claude_authenticated=False, codex_authenticated=False
    )
    env["SSH_CONNECTION"] = "client server"

    result = _run_interactive_setup(
        cwd=tmp_path,
        env=env,
        args=("--yes",),
        answers="n\nn\nn\n\n",
    )

    assert result.returncode == 0, result.stdout
    assert "Start Claude Code sign-in now? [y/N]" in result.stdout
    assert "Start Codex sign-in now? [y/N]" in result.stdout
    provider_calls = provider_log.read_text().splitlines()
    assert "claude auth login" not in provider_calls
    assert "codex login --device-auth" not in provider_calls


def test_noninteractive_setup_prints_pending_login_without_launching_it(tmp_path: Path) -> None:
    _, _, provider_log, _, env = _guided_setup_environment(
        tmp_path, claude_authenticated=False, codex_authenticated=False
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "setup.sh")],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "claude auth login" in result.stdout
    assert "codex login --device-auth" in result.stdout
    provider_calls = provider_log.read_text().splitlines()
    assert "claude auth login" not in provider_calls
    assert "codex login --device-auth" not in provider_calls


def test_locked_dependencies_are_installed_before_editable_package(tmp_path: Path) -> None:
    home, python_log, _, _, env = _guided_setup_environment(tmp_path)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    assert "Installing locked dependencies (the first run can take a few minutes)" in result.stdout
    assert "Installing Agent Duet itself" in result.stdout
    pip_calls = [line for line in python_log.read_text().splitlines() if " -m pip " in line]
    managed_python = tmp_path / "home/.local/share/agent-duet/venv/bin/python"
    assert pip_calls == [
        f"{managed_python} -m pip install --disable-pip-version-check -r "
        f"{PROJECT_ROOT / 'requirements-lock.txt'}",
        f"{managed_python} -m pip install --disable-pip-version-check --no-deps "
        f"--editable {PROJECT_ROOT}",
    ]
    launcher = home / ".local/bin/agent-duet"
    assert launcher.is_symlink()
    assert launcher.resolve() == managed_python.parent / "agent-duet"


def test_guided_setup_runs_health_check_before_finishing(tmp_path: Path) -> None:
    _, python_log, _, _, env = _guided_setup_environment(tmp_path)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    assert "Installation checks passed." in result.stdout
    assert "Project and managed ask/deny rules remain authoritative" in result.stdout
    assert any(
        "agent-duet doctor" in line for line in python_log.read_text().splitlines()
    )


def test_guided_setup_tells_users_to_restart_open_clients(tmp_path: Path) -> None:
    _, _, _, _, env = _guided_setup_environment(tmp_path)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    assert "Close and reopen any Claude Code or Codex sessions" in result.stdout
    assert "keep the old MCP process and /duet instructions" in result.stdout


def test_install_adds_only_exact_read_only_claude_poll_permissions(tmp_path: Path) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert settings["permissions"]["allow"] == list(POLL_PERMISSIONS)
    assert "mcp__agent_duet__*" not in settings["permissions"]["allow"]
    assert "read-only polling permissions configured" in result.stdout


def test_install_preserves_existing_claude_settings(tmp_path: Path) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)
    settings_path = home / ".claude/settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["Bash(pacman -Q*)"],
                    "defaultMode": "auto",
                },
                "theme": "dark",
            }
        )
        + "\n"
    )

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    settings = json.loads(settings_path.read_text())
    assert settings == {
        "permissions": {
            "allow": ["Bash(pacman -Q*)", *POLL_PERMISSIONS],
            "defaultMode": "auto",
        },
        "theme": "dark",
    }
    backup = Path(f"{settings_path}.duet-backup")
    assert json.loads(backup.read_text())["permissions"]["allow"] == [
        "Bash(pacman -Q*)"
    ]


def test_install_permission_repair_is_idempotent(tmp_path: Path) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)

    first = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")
    assert first.returncode == 0, first.stdout
    settings_path = home / ".claude/settings.json"
    before = settings_path.read_bytes()
    before_mtime = settings_path.stat().st_mtime_ns

    second = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert second.returncode == 0, second.stdout
    assert settings_path.read_bytes() == before
    assert settings_path.stat().st_mtime_ns == before_mtime
    assert json.loads(settings_path.read_text())["permissions"]["allow"] == list(
        POLL_PERMISSIONS
    )


def test_install_refuses_malformed_claude_settings_without_damage(
    tmp_path: Path,
) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)
    settings_path = home / ".claude/settings.json"
    settings_path.parent.mkdir(parents=True)
    original = b'{"permissions": {"allow": ['
    settings_path.write_bytes(original)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 1, result.stdout
    assert "could not parse Claude Code settings" in result.stdout
    assert "could not configure Claude Code's read-only" in result.stdout
    assert settings_path.read_bytes() == original
    assert not Path(f"{settings_path}.duet-backup").exists()


def test_check_fails_when_a_claude_poll_permission_is_missing(tmp_path: Path) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)
    installed = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")
    assert installed.returncode == 0, installed.stdout
    settings_path = _write_claude_poll_permissions(home)
    settings = json.loads(settings_path.read_text())
    settings["permissions"]["allow"].remove(POLL_PERMISSIONS[1])
    settings_path.write_text(json.dumps(settings) + "\n")

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "setup.sh"), "check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout
    assert "polling permissions are missing" in result.stdout
    assert "run ./setup.sh install" in result.stdout.lower()


@pytest.mark.parametrize(
    ("policy", "rule"),
    [
        ("ask", POLL_PERMISSIONS[0]),
        ("deny", "mcp__agent_duet__*"),
    ],
)
def test_check_fails_for_higher_precedence_claude_polling_rule(
    tmp_path: Path, policy: str, rule: str
) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)
    installed = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")
    assert installed.returncode == 0, installed.stdout
    settings_path = home / ".claude/settings.json"
    settings = json.loads(settings_path.read_text())
    settings["permissions"][policy] = [rule]
    settings_path.write_text(json.dumps(settings) + "\n")

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "setup.sh"), "check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout
    assert f"permissions.{policy}" in result.stdout
    assert "Everything works." not in result.stdout


def test_uninstall_removes_only_claude_poll_permissions(tmp_path: Path) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)
    settings_path = _write_claude_poll_permissions(home, "Bash(pacman -Q*)")
    installed = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")
    assert installed.returncode == 0, installed.stdout
    assert all(
        rule in json.loads(settings_path.read_text())["permissions"]["allow"]
        for rule in POLL_PERMISSIONS
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "setup.sh"), "uninstall"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    settings = json.loads(settings_path.read_text())
    assert settings["permissions"]["allow"] == ["Bash(pacman -Q*)"]
    assert "removed read-only Claude polling permissions" in result.stdout


def test_health_check_surfaces_missing_project_warning_without_failing(tmp_path: Path) -> None:
    home, env = _fake_install_environment(tmp_path)
    _write_claude_poll_permissions(home)
    fake_agent_duet = tmp_path / "bin/agent-duet"
    missing_project = tmp_path / "projects/moved-project"
    _write_executable(
        fake_agent_duet,
        f"""#!/usr/bin/env bash
if [[ "$1" == "doctor" ]]; then
    echo "agent-duet 1.0.0"
    echo "  repo {missing_project}: MISSING, 1 validation command(s)"
    echo "    WARNING: registered project directory is missing: {missing_project}. Restore it, or run: ./setup.sh remove-repo {missing_project}"
    echo "result: OK (1 warning(s))"
    exit 0
fi
""",
    )
    codex_home = home / ".codex"
    claude_commands = home / ".claude/commands"
    codex_home.joinpath("prompts").mkdir(parents=True)
    claude_commands.mkdir(parents=True)
    codex_home.joinpath("prompts/duet.md").write_text("installed")
    claude_commands.joinpath("duet.md").write_text("installed")
    codex_home.joinpath("config.toml").write_text(
        f'''[mcp_servers.agent_duet]
command = "{fake_agent_duet}"
tool_timeout_sec = 120
enabled_tools = ["duet_start", "duet_status", "duet_wait", "duet_cancel", "duet_finalize"]
'''
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "setup.sh"), "check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout
    assert f"registered project directory is missing: {missing_project}" in result.stdout
    assert "Installation checks passed with warning(s). Review them above." in result.stdout
    assert "Something is off" not in result.stdout


def test_failed_health_check_does_not_report_setup_complete(tmp_path: Path) -> None:
    home, _, _, _, env = _guided_setup_environment(tmp_path)
    _write_executable(
        tmp_path / "provider-templates/claude",
        "#!/bin/bash\n"
        '[[ "$*" == "--version" ]] && echo "2.1.236 (Claude Code)"\n'
        '[[ "$*" == "auth status" ]] && exit 0\n'
        "exit 0\n",
    )

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="")

    assert result.returncode == 1, result.stdout
    assert "not connected" in result.stdout
    assert "Something is off. Run ./setup.sh to repair it." in result.stdout
    assert "Setup is done" not in result.stdout
    assert (home / ".config/agent-duet/config.toml").is_file()


def test_codex_health_uses_validated_config_not_cli_display_text(tmp_path: Path) -> None:
    _, _, _, _, env = _guided_setup_environment(tmp_path)
    _write_executable(
        tmp_path / "provider-templates/codex",
        "#!/bin/bash\n"
        '[[ "$*" == "--version" ]] && echo "codex-cli 0.153.2"\n'
        '[[ "$*" == "login status" ]] && exit 0\n'
        '[[ "$*" == "mcp get agent_duet" ]] && echo "agent_duet: enabled"\n'
        "exit 0\n",
    )

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    assert "connected, all five tools enabled" in result.stdout
    assert "not registered properly" not in result.stdout


def test_codex_health_rejects_config_without_all_five_tools(tmp_path: Path) -> None:
    home, env = _fake_install_environment(tmp_path)
    codex_home = home / ".codex"
    claude_commands = home / ".claude/commands"
    codex_home.joinpath("prompts").mkdir(parents=True)
    claude_commands.mkdir(parents=True)
    codex_home.joinpath("prompts/duet.md").write_text("installed")
    claude_commands.joinpath("duet.md").write_text("installed")
    codex_home.joinpath("config.toml").write_text(
        f'''[mcp_servers.agent_duet]
command = "{tmp_path / "bin/agent-duet"}"
tool_timeout_sec = 120
enabled_tools = ["duet_start", "duet_status", "duet_wait", "duet_cancel"]
'''
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "setup.sh"), "check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout
    assert "not registered properly" in result.stdout


def test_codex_health_rejects_directory_as_command(tmp_path: Path) -> None:
    home, env = _fake_install_environment(tmp_path)
    codex_home = home / ".codex"
    claude_commands = home / ".claude/commands"
    codex_home.joinpath("prompts").mkdir(parents=True)
    claude_commands.mkdir(parents=True)
    codex_home.joinpath("prompts/duet.md").write_text("installed")
    claude_commands.joinpath("duet.md").write_text("installed")
    codex_home.joinpath("config.toml").write_text(
        f'''[mcp_servers.agent_duet]
command = "{tmp_path / "bin"}"
tool_timeout_sec = 120
enabled_tools = ["duet_start", "duet_status", "duet_wait", "duet_cancel", "duet_finalize"]
'''
    )

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "setup.sh"), "check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout
    assert "not registered properly" in result.stdout


def test_claude_health_check_handles_large_trailing_output(tmp_path: Path) -> None:
    _, _, _, _, env = _guided_setup_environment(tmp_path)
    _write_executable(
        tmp_path / "provider-templates/claude",
        "#!/bin/bash\n"
        '[[ "$*" == "--version" ]] && echo "2.1.236 (Claude Code)"\n'
        '[[ "$*" == "auth status" ]] && exit 0\n'
        'if [[ "$*" == "mcp get agent_duet" ]]; then\n'
        '    echo "Connected"\n'
        "    for ((i = 0; i < 20000; i++)); do echo \"trailing output $i\"; done\n"
        "fi\n"
        "exit 0\n",
    )

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="n\n\n")

    assert result.returncode == 0, result.stdout
    assert "Can Claude Code see it?\n    ✓ connected" in result.stdout


def test_unsupported_os_fails_before_creating_python_environment(tmp_path: Path) -> None:
    home, python_log, _, env = _python_setup_environment(tmp_path, with_conda=False)
    _write_executable(tmp_path / "bin/uname", "#!/bin/bash\necho Darwin\n")

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\n")

    assert result.returncode == 1, result.stdout
    assert "supports Linux only" in result.stdout
    calls = python_log.read_text().splitlines() if python_log.exists() else []
    assert not [call for call in calls if " -m venv " in call]
    assert not (home / ".local/share/agent-duet/venv").exists()


def test_missing_downloader_fails_before_creating_python_environment(tmp_path: Path) -> None:
    home, python_log, _, env = _python_setup_environment(tmp_path, with_conda=False)
    fake_bin = tmp_path / "bin"
    (fake_bin / "claude").unlink()
    _write_executable(fake_bin / "uname", "#!/bin/bash\necho Linux\n")
    _write_executable(fake_bin / "git", "#!/bin/bash\nexit 0\n")
    (fake_bin / "dirname").symlink_to("/usr/bin/dirname")
    env["PATH"] = str(fake_bin)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\n")

    assert result.returncode == 1, result.stdout
    assert "curl or wget" in result.stdout
    calls = python_log.read_text().splitlines() if python_log.exists() else []
    assert not [call for call in calls if " -m venv " in call]
    assert not (home / ".local/share/agent-duet/venv").exists()


def test_missing_git_fails_before_creating_python_environment(tmp_path: Path) -> None:
    home, python_log, _, env = _python_setup_environment(tmp_path, with_conda=False)
    fake_bin = tmp_path / "bin"
    _write_executable(fake_bin / "uname", "#!/bin/bash\necho Linux\n")
    (fake_bin / "dirname").symlink_to("/usr/bin/dirname")
    env["PATH"] = str(fake_bin)

    result = _run_interactive_setup(cwd=tmp_path, env=env, answers="y\n")

    assert result.returncode == 1, result.stdout
    assert "git was not found" in result.stdout
    calls = python_log.read_text().splitlines() if python_log.exists() else []
    assert not [call for call in calls if " -m venv " in call]
    assert not (home / ".local/share/agent-duet/venv").exists()


def test_sourcing_setup_does_not_dispatch_commands(tmp_path: Path) -> None:
    marker = tmp_path / "sourced"

    result = subprocess.run(
        ["bash", "-c", f'source "{PROJECT_ROOT / "setup.sh"}"; touch "{marker}"'],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.is_file()
    assert "Checking what is already on this machine" not in result.stdout


def test_help_contains_only_the_header_comment() -> None:
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "setup.sh"), "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "guided setup" in result.stdout
    assert "set -euo pipefail" not in result.stdout


def test_documentation_leads_with_short_guided_install_and_consent_disclosure() -> None:
    install = (PROJECT_ROOT / "INSTALL.md").read_text()
    readme = (PROJECT_ROOT / "README.md").read_text()
    expected_commands = (
        "git clone https://github.com/slyfox1186/Claude_Code_Codex_MCP_Coordinator.git",
        "cd Claude_Code_Codex_MCP_Coordinator",
        "./setup.sh",
    )

    for document in (install, readme):
        for command in expected_commands:
            assert command in document
        assert "consent" in document.lower()
        assert "agent-duet" in document
        assert "Conda" in document
        assert "never installs Conda" in document
        assert "SECURITY.md" in document
        assert "-d" in document
        assert "--directory" in document
        assert "does not need to be a Git repository" in document or "ordinary folder" in document
        assert "requirements-dev.txt" in document
        assert "project" in document.lower() and "environment" in document.lower()
        assert "pytest" in document.lower()

    assert readme.index("## Install") < readme.index("## Installing by hand")


def test_documentation_explains_claude_polling_permission_boundary() -> None:
    documents = {
        name: (PROJECT_ROOT / name).read_text()
        for name in ("README.md", "INSTALL.md", "HOW_TO_BUILD_THIS.md", "HOW_TO_TEST.md")
    }

    for name, text in documents.items():
        assert "read-only" in text.lower(), name
        assert "mcp__agent_duet__duet_status" in text, name
        assert "mcp__agent_duet__duet_wait" in text, name
        assert "Denied by auto mode classifier" in text, name
        assert "./setup.sh install" in text, name
        assert "/permissions" in text, name

    manual_setup = documents["README.md"].split("## Installing by hand", 1)[1]
    assert '"mcp__agent_duet__duet_status"' in manual_setup
    assert '"mcp__agent_duet__duet_wait"' in manual_setup
    assert "303 passed" not in documents["HOW_TO_BUILD_THIS.md"]
    assert "14 source files" not in documents["HOW_TO_BUILD_THIS.md"]
