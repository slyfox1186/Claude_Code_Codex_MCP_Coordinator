# INSTALL

On a Linux server, run:

```bash
git clone https://github.com/slyfox1186/Claude_Code_Codex_MCP_Coordinator.git
cd Claude_Code_Codex_MCP_Coordinator
./setup.sh -d /path/to/your/project
```

`./setup.sh --directory /path/to/your/project` is identical. Omit the option only if you
want setup to ask for the project path later.

Follow the prompts. Setup explains each change and asks for consent before it:

The first dependency install can take a few minutes. Setup keeps the download progress visible.

- If Conda is detected, setup creates a dedicated environment named `agent-duet`.
  Setup never installs Conda or changes `base` or any existing environment.
- Without Conda, setup uses the default Python 3.13+ only to create a private Agent Duet
  environment. It does not install packages into system Python.
- If Claude Code or Codex is missing, setup offers their official installer and waits for `y`.
  Before asking, it shows the expected user-local files. These installers manage their own
  updates, and Codex may add its bin directory to your shell profile's `PATH`.
- `-d` or `--directory` accepts a relative path, full path, `~/...`, or a trailing `/`
  and skips the demo and project-path questions.
- The project does not need to be a Git repository beforehand. Setup automatically
  creates the local baseline Agent Duet needs to compare Claude's and Codex's work by
  committing the existing non-ignored files. Nothing is uploaded and no remote is added.
  Review `.gitignore` first if sensitive files exist.

Check the finished installation:

```bash
./setup.sh check
```

Close and reopen any Claude Code or Codex sessions that were already running during
setup. Open clients retain the old MCP process and `/duet` instructions loaded at startup.

New installations allow two different projects to run concurrently. Change
`max_parallel_global` in `~/.config/agent-duet/config.toml` to select 1 through 16.

Requirements: Linux and Git. Without Conda, the server also needs Python 3.13 or newer.
`curl` or `wget` is needed only when a provider CLI must be downloaded.

If you decline a required change, nothing from that step is installed; rerun `./setup.sh` when
ready. If sign-in was skipped, run the command setup printed. For deeper diagnostics, run
`agent-duet doctor`.

| Problem | What to do |
|---|---|
| Python is older than 3.13 and Conda is absent | Install Python 3.13+, then rerun `./setup.sh` |
| Signing in over SSH/headless | Run `codex login --device-auth` |
| A check fails | Run `agent-duet doctor` for the detailed reason |

Agent sessions receive broad access to your machine. Read [SECURITY.md](SECURITY.md) before use.
