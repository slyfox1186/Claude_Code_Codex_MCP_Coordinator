# Guided Installer Design

**Date:** 2026-09-04

## Objective

Make a fresh Linux installation of Agent Duet a three-command experience:

```bash
git clone https://github.com/slyfox1186/Claude_Code_Codex_MCP_Coordinator.git
cd Claude_Code_Codex_MCP_Coordinator
./setup.sh
```

The guided installer must detect missing prerequisites, explain every external change, obtain
consent, finish all Agent Duet setup, and verify the result. `README.md` and a new `INSTALL.md`
must make that behavior explicit without burying the normal path in manual instructions.

## Chosen Approach

Extend the existing `setup.sh` instead of adding a second bootstrap script. One entry point is
easier to discover, preserves the current interface, and lets existing repair, repository,
demo, and uninstall commands reuse the same detection logic.

The default `./setup.sh` command is the guided fresh-install path. `./setup.sh install` remains
the deterministic repair path for machines whose prerequisites already exist. `--yes` counts as
explicit command-line consent for prerequisite installation; without it, every third-party
installation or login launch requires its own prompt. Authentication still requires the user to
complete the provider's flow.

## Supported Boundary

The installer manages user-scoped software only:

- a dedicated Conda environment named `agent-duet` when Conda is already installed;
- otherwise, a private virtual environment created from the default `python3` interpreter;
- Claude Code through Anthropic's official Linux installer;
- Codex through OpenAI's official Linux installer;
- Agent Duet, its locked Python dependencies, configuration, MCP registrations, and command files.

It never installs Conda, invokes `sudo`, changes Conda's `base` environment, or installs packages
into system Python. Git is required; Python 3.13+ is required when Conda is absent; and either
`curl` or `wget` is required when a provider CLI is missing. Missing system-level tools produce
one short distro-neutral error.

Creating or repairing an isolated Python environment and installing either vendor CLI requires
consent. Declining a required step stops cleanly and prints the one next command or action needed.
Declining an optional login offer finishes installation but clearly reports that Agent Duet cannot
run that provider until the user signs in.

## Guided Flow

1. Before any mutation, detect Linux and Git, and require a supported download command when a
   provider CLI is missing. When Conda is absent, also require Python 3.13+ before environment
   creation.
2. Detect Conda through `CONDA_EXE`, an executable returned by `command -v conda`, or
   `$HOME/miniconda3/bin/conda`.
3. When Conda exists, show the exact command and ask before creating a dedicated environment named
   `agent-duet` with Python 3.13 and pip. Reuse that environment when it is already compatible;
   repair only that named environment when necessary. Resolve its actual interpreter through
   `conda run` so configured environment directories work. Never modify `base` or another
   environment.
4. When Conda does not exist, require a compatible default `python3`, show the private environment
   path, and ask before creating an Agent Duet virtual environment under the user's data directory.
   Never reuse an existing launcher or `DUET_PYTHON`, and never pass pip operations to the system
   interpreter itself.
5. If Claude Code is absent, show Anthropic's official installer URL, expected user-local files,
   shell integration, and self-update behavior, then ask before running it.
6. If Codex is absent, show OpenAI's official installer URL, effective install/package paths, and
   the shell profile where it may add a managed `PATH` block, then ask before running it.
7. Check authentication with `claude auth status` and `codex login status`. If either CLI is not
   authenticated, offer to start its login. Use `claude auth login` for Claude. Use
   `codex login --device-auth` for SSH/headless sessions and `codex login` otherwise. If the
   installer has no interactive terminal, print these commands instead of starting a login.
8. Install `requirements-lock.txt` with the selected absolute Python, then install Agent Duet in
   editable mode with `--no-deps`. Keep the repository in place because the editable install
   points to it.
9. Generate the private Agent Duet config and state directories, register the stdio MCP server
   with both clients, install both `/duet` command files, and run the existing health check.
10. Offer the existing disposable demo. If the user declines, immediately ask for the real
    project's Git repository path. Accept an absolute path, a path relative to the directory from
    which setup was launched, or a leading `~/` path without using `eval`. Blank input explicitly
    skips repository registration and prints the later `add-repo` command.

Every vendor CLI download goes to a `mktemp -d` directory, uses HTTPS, executes only after consent,
and is removed on exit. The installer prints the source URL and install target before asking. It
never logs, copies, or requests an API key.

## Command Behavior

- `./setup.sh`: guided prerequisite bootstrap, Agent Duet install, verification, optional demo.
- `./setup.sh install`: install and repair Agent Duet; report missing prerequisites without
  downloading them.
- `./setup.sh --yes`: explicit blanket consent for prerequisite installations only. It never
  consents to demo creation or deletion. A non-interactive run still leaves provider authentication
  for the user and prints the required login commands.
- Existing `add-repo`, `remove-repo`, `check`, `demo`, and `uninstall` behavior remains compatible.

Prompts for third-party changes use a conservative `[y/N]` default. The existing optional demo
prompt may retain `[Y/n]` because it only creates a disposable local repository.

## Documentation

Create `INSTALL.md` with:

- the three normal commands at the top;
- a short list of what the installer may offer to install;
- an explicit statement that each third-party change requires consent;
- the expected headless login behavior;
- one verification command and a small troubleshooting table;
- links to `SECURITY.md` and the longer operational sections of `README.md`.

Replace the `README.md` installation section with the same short path and consent statement. Keep
manual installation as an advanced reference, not the default journey. Update any existing claim
that setup never prompts or that users must manually preinstall all prerequisites.

## Failure Handling

- Unsupported operating system: stop before modification and name Linux as the supported target.
- Missing Git, Python 3.13+, Python's `venv` support, or a download command: stop with one concise
  prerequisite instruction.
- Declined required installation: make no change for that prerequisite, print `installation not
  completed`, and exit with status 2 so callers cannot mistake refusal for a completed install.
- Download, installer, authentication, pip, registration, or health-check failure: stop at the
  failed stage, preserve vendor output, and print the exact recovery command.
- Existing compatible tools: never reinstall or upgrade them during a normal guided run.
- Existing Agent Duet configuration: preserve it using the current backup and validation rules.

## Testing

Add focused automated tests that run `setup.sh` with an isolated temporary home and fake commands.
No test may use the network, modify real CLI configuration, or install real packages.

The tests must prove:

- a Conda installation is never downloaded or offered;
- detected Conda creates or reuses only the named `agent-duet` environment and never changes base;
- without Conda, the default compatible `python3` creates a private Agent Duet virtual environment;
- an old or incomplete system Python produces a concise error rather than a Conda installation;
- a required environment is never created after the user declines;
- `--yes` is treated as explicit consent;
- `install` remains non-bootstrapping;
- compatible existing prerequisites are not reinstalled;
- headless Codex authentication selects device-code login;
- locked dependencies are installed before the editable package;
- temporary downloads are removed on success and failure;
- declining the demo prompts for a real repository and registers relative, absolute, and `~/`
  paths correctly;
- blank repository input skips registration without registering the Agent Duet source repository;
- the short README and `INSTALL.md` describe consent and the three-command path.

After focused tests, run the full pytest suite, Ruff, mypy, `bash -n setup.sh`, and isolated smoke
tests for guided acceptance and refusal paths. Inspect the rendered Markdown and the complete diff.

## Sources

- Project behavior and current installer: `README.md`, `SECURITY.md`, `setup.sh`,
  `requirements-lock.txt`, and `HOW_TO_BUILD_THIS.md` at commit `a574b78`.
- Anthropic Claude Code installation and SSH authentication:
  <https://code.claude.com/docs/en/quickstart> and
  <https://code.claude.com/docs/en/troubleshoot-install>.
- OpenAI Codex Linux installation and headless authentication:
  <https://learn.chatgpt.com/docs/codex/cli> and <https://learn.chatgpt.com/docs/auth>.
