# How to install agent-duet on a second machine

Give this whole file to Claude Code on the target PC. It is written as an instruction
set for an agent, not as prose for a person: every step has a command and a check, and
the checks are what decide whether the step passed.

Do not skip a check. If one fails, stop and report the exact command, its exit code, and
its output. Do not work around a failure by loosening a setting.

---

## 0. What you are installing

`agent-duet` is a local stdio **MCP coordinator**. Registered in both Claude Code and the
Codex CLI, it runs one workflow on request:

1. a fresh Claude Code process implements a task and writes a handoff file;
2. a fresh Codex process reviews it independently and returns a critique;
3. a fresh Claude Code process adjudicates that critique, fixes what is justified, and
   revalidates;
4. the run stops at `AWAITING_FINALIZE` and waits for a human;
5. a separate tool call commits, pushes, and verifies the remote SHA.

`duet_start` cannot commit, push, or deploy. Only `duet_finalize` can, and only after
re-verifying the branch, the remote URL, and the exact git tree that was validated.

**Read `SECURITY.md` before installing.** The child agents run with full, unsandboxed
machine access by deliberate design. That is the shipped default and it is intentional;
do not change it unless the owner says so.

---

## 1. Preflight

```bash
git --version
claude --version
codex --version
command -v claude
command -v codex
```

**Checks**

- `git` is 2.30 or newer.
- Both CLIs print a version. Tested against Claude Code `2.1.236` and Codex `0.153.2`.
- Interactive `claude` and `codex` sessions already work and are logged in. agent-duet
  reuses their existing authentication and never handles credentials itself.

Now pick the interpreter. **Use the same Python that owns your other tooling.** On
Jeff's machines that is Miniconda:

```bash
PY=/home/$USER/miniconda3/bin/python
"$PY" -V
```

**Check:** it prints `Python 3.13.x` or newer. If it prints 3.12 or older, stop and say
so; do not silently install into a different interpreter.

Export `PY` for the rest of this document. Every later command uses it.

---

## 2. Clone

```bash
git clone https://github.com/slyfox1186/Claude_Code_Codex_MCP_Coordinator.git \
  "$HOME/src/agent-duet"
cd "$HOME/src/agent-duet"
git log --oneline -1
```

**Check:** the clone succeeds and you are on `main`.

The repository is private. If the clone prompts for credentials, use the GitHub CLI's
existing login rather than typing a token:

```bash
gh auth status          # must show a logged-in account with 'repo' scope
gh auth setup-git       # makes git reuse that login
```

Never put a token in the remote URL, in a file, or in a command line.

---

## 3. Install

Note the layout before you touch anything: **the modules live directly in `src/`**
(`src/server.py`, `src/worker.py`, `src/prompts/`). There is deliberately no
`src/agent_duet/` directory. `pyproject.toml` maps the directory onto the import name:

```toml
[tool.setuptools]
packages = ["agent_duet"]
package-dir = { "agent_duet" = "src" }
```

Do not "fix" this by creating a nested package directory. It is the intended layout.

```bash
cd "$HOME/src/agent-duet"
"$PY" -m pip install -r requirements-lock.txt
"$PY" -m pip install -e ".[dev]"
```

**Checks**

```bash
"$PY" -c "import agent_duet; print(agent_duet.__file__)"
command -v agent-duet
agent-duet --version
```

- The import path ends in `.../agent-duet/src/__init__.py`.
- `agent-duet` resolves to a real path next to your `$PY` (for example
  `/home/$USER/miniconda3/bin/agent-duet`). Write that path down; you need it twice below.
- `agent-duet --version` prints `agent-duet 1.0.0`.

There is no virtualenv and no `.venv/`. If you find yourself creating one, stop.

---

## 4. Run the test suite

```bash
cd "$HOME/src/agent-duet"
"$PY" -m pytest
"$PY" -m ruff check .
"$PY" -m mypy
```

**Checks**

- pytest: **303 passed**, zero failures. The suite drives the real worker, the real state
  machine, and real git operations; only the two model CLIs are stand-ins.
- ruff: `All checks passed!`
- mypy: `Success: no issues found in 14 source files`

If pytest fails, stop and report the failing test names and their assertion output. A red
suite means the install is not usable, not that the tests are wrong.

---

## 5. Configure

```bash
mkdir -p "$HOME/.config/agent-duet" "$HOME/.local/state/agent-duet"
cp config.example.toml "$HOME/.config/agent-duet/config.toml"
chmod 700 "$HOME/.config/agent-duet" "$HOME/.local/state/agent-duet"
chmod 600 "$HOME/.config/agent-duet/config.toml"
```

Edit `$HOME/.config/agent-duet/config.toml` and replace **every** `REPLACE_ME`:

| Key | Value to use |
|---|---|
| `allowed_repo_roots` | the directory your projects live under, e.g. `["/home/YOU/tmp"]` |
| `state_dir` | `/home/YOU/.local/state/agent-duet` |
| `[claude] executable` | output of `command -v claude` |
| `[codex] executable` | output of `command -v codex` |

Rules the loader enforces, so get them right the first time:

- Unknown keys are a hard error. A typo fails loudly instead of being ignored.
- `allowed_repo_roots` entries must be absolute, and may not be `/` or a home directory
  itself. A run target must sit *strictly below* a listed root.
- The config file must be a regular file **you own**, not a symlink, and not group- or
  world-writable. It names the executables to run and the exact command vectors the
  coordinator will execute, so write access to it is equivalent to code execution.

Optional but recommended, per repository you intend to use:

```toml
[[repositories]]
path = "/home/YOU/tmp/some-project"
validation_commands = [
  ["/home/YOU/miniconda3/bin/python", "-m", "pytest", "-q"],
]
validation_timeout_seconds = 600
```

These are the coordinator's own authoritative checks, run after the agents finish. They
are command **vectors**, never shell strings, and they are read only from this file —
never from task text or from a file inside the repository under test.

Then:

```bash
agent-duet doctor
```

**Check:** the last line is `result: OK`. It also prints both CLI versions, the resolved
executable paths, the state directory mode (`0o700`), and the SQLite status. It never
prints a credential. If it reports a problem, fix that problem before continuing.

---

## 6. Register with both CLIs

Use the absolute path from step 3.

```bash
DUET_BIN="$(command -v agent-duet)"

claude mcp add-json --scope user agent_duet \
  "{\"type\":\"stdio\",\"command\":\"$DUET_BIN\",\"args\":[],\"env\":{},\"timeout\":330000}"

codex mcp add agent_duet -- "$DUET_BIN"
```

Then open `$HOME/.codex/config.toml` and extend the `agent_duet` table that
`codex mcp add` just created. Do not add a second table:

```toml
[mcp_servers.agent_duet]
command = "/absolute/path/to/agent-duet"
args = []
startup_timeout_sec = 20
# duet_wait blocks for up to 300s, so the tool timeout must sit above that.
tool_timeout_sec = 330
enabled_tools = ["duet_start", "duet_status", "duet_wait", "duet_cancel", "duet_finalize"]
```

**Checks**

```bash
claude mcp get agent_duet     # Status: ✔ Connected
codex mcp get agent_duet      # enabled_tools lists all five
```

Then start each CLI interactively and run `/mcp`. Both must show `agent_duet` with
**exactly five** tools: `duet_start`, `duet_status`, `duet_wait`, `duet_cancel`,
`duet_finalize`.

The 330 000 ms / 330 s timeouts are not decoration. `duet_wait` deliberately blocks for
up to 300 s, and a client timeout below that will kill a healthy call.

---

## 6b. Install the /duet slash command

```bash
cd "$HOME/src/agent-duet"
./scripts/install-slash-command.sh
```

**Checks**

```bash
ls -l "$HOME/.claude/commands/duet.md" "$HOME/.codex/prompts/duet.md"
```

Both exist. In an interactive session, typing `/duet` offers the command.

The script only copies `commands/duet.md` into those two config directories. It needs no
elevation, and it backs up any existing `duet.md` before overwriting. `--claude` and
`--codex` limit it to one CLI; `--uninstall` removes what it installed.

---

## 7. Prove it end to end on a disposable repository

Do **not** make the first run against anything you care about.

```bash
rm -rf "$HOME/tmp/duet-smoke" && mkdir -p "$HOME/tmp/duet-smoke"
cd "$HOME/tmp/duet-smoke"
git init -q -b main .
git config user.email "you@example.com"
git config user.name "You"
printf '# duet-smoke\n' > README.md
printf '#!/usr/bin/env python3\n"""Tiny module."""\n\n\ndef multiply(a, b):\n    """Return a * b."""\n    return a * b\n' > calc.py
printf '#!/usr/bin/env python3\n"""Tests."""\n\nfrom calc import multiply\n\n\ndef test_multiply():\n    assert multiply(3, 4) == 12\n' > test_calc.py
git add -A && git commit -q -m "initial"
git rev-parse HEAD
```

Add it to your config as a `[[repositories]]` entry with a real validation command (see
step 5), re-run `agent-duet doctor`, then start an interactive Claude Code session in
that directory and say:

```
Use agent_duet to run the full Claude->Codex->Claude workflow in this repository.

Task: Add a pure function named add(a, b) to calc.py and tests for integers and floats.
Acceptance criteria:
- existing tests still pass
- new tests cover both cases
- stop at AWAITING_FINALIZE
- summarize the evidence and ask me before finalizing
```

**Checks — every one of these must hold**

- Exactly **one** run id is created. The model calls `duet_start` once and reuses the id.
- `duet_start` returns in seconds, not minutes. It spawns a detached worker and returns.
- The phases appear in order:
  `QUEUED → CLAUDE_IMPLEMENTING → HANDOFF_VALIDATING → CODEX_REVIEWING →
  REVIEW_INTEGRITY_CHECK → CLAUDE_RECONCILING → FINAL_VALIDATING → AWAITING_FINALIZE`.
- Claude performs both write phases; Codex performs exactly one review.
- `CLAUDE_CRITIQUE_REQUEST.md` is written by Claude, and `GPT_CRITIQUE_FOR_CLAUDE.md` is
  written by **the coordinator**, never by Codex.
- Both files are archived into the run directory and **removed** from the worktree before
  final validation, so neither can reach a commit.
- The original repository's `HEAD` is unchanged. All work happens in a private worktree on
  an `agent-duet/<short-id>` branch.
- The run stops at `AWAITING_FINALIZE`. Nothing is committed, pushed, or deployed.

Inspect the evidence directly:

```bash
ls -la "$HOME/.local/state/agent-duet/runs/<run-id>/"
cat "$HOME/.local/state/agent-duet/runs/<run-id>/validation-manifest.json"
```

**Checks:** the run directory is `0700`, files are `0600`, the prompts and logs contain no
credentials, and the manifest records each validation's argv, exit code, and timing.

### Prove the run survives the client

Close the Claude Code session entirely. Open a new one anywhere and say:

```
Call duet_status for run <run-id>.
```

**Check:** it returns the same run at the same phase. The worker is detached; it does not
belong to the client that started it.

### Prove cancellation reaps the child

Start another run, and while a phase is mid-flight:

```
Cancel run <run-id>.
```

**Check:** the run becomes `CANCELLED`, and no orphaned `claude` or `codex` process is
left behind:

```bash
ps -ef | grep -E "claude|codex" | grep -v grep
```

This is worth testing explicitly. Each child agent runs in its own process group, so
signalling only the worker would leave a fully privileged agent running. The coordinator
records the active child's group and reaps it first.

---

## 8. Finalize, only when you mean it

From the interactive session, once you have read the evidence:

```
Finalize run <run-id> only if its validated diff is unchanged.
Expected branch: <branch>. Expected remote: origin.
Expected remote URL: <exact URL>. Commit message: <message>.
Push it, verify the remote ref equals the exact commit, and report exact evidence.
```

**Checks**

- The returned `local_commit_sha` and `remote_commit_sha` are identical 40-character SHAs.
- `git ls-remote <remote> refs/heads/<branch>` from a separate shell returns that same SHA.
- The commit contains only the run-owned paths, and neither critique file.
- With no deployment verifier configured, deployment is reported as `NOT_CHECKED`. That is
  correct. It is never reported as successful without machine-readable evidence
  containing the exact pushed SHA.

Finalization refuses, by design, when: the run is not exactly `AWAITING_FINALIZE`; the
branch does not match; the remote URL does not match both your request **and** the URL
recorded when the run started; the validated git tree changed; a coordination artifact is
present; or a changed file contains credential-shaped content.

---

## 9. Two-machine rules

- Install the same commit on both PCs. Verify with `git rev-parse HEAD`.
- Configuration is machine-local. Executable and repository paths differ; do not copy
  `config.toml` between machines without re-checking every path.
- **Never** share `~/.local/state/agent-duet` between machines, and never put it on NFS,
  Dropbox, or Syncthing. The two machines coordinate through git remotes, not through this
  database.
- Do not run workflows against the same branch from both PCs at once. Prefer the unique
  `agent-duet/<run-id>` branches and merge through a pull request.
- Enable a deployment profile only on the machine that actually owns deployment.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'agent_duet'` | someone created `src/agent_duet/` | move the modules back up into `src/` and reinstall |
| MCP server will not connect | the stored command is not an absolute path | re-register with `$(command -v agent-duet)` |
| Tool call times out around 60 s | client tool timeout below 330 s | set `tool_timeout_sec = 330` / `"timeout": 330000` |
| `Error loading config.toml: invalid transport` from Codex | an MCP override was passed alongside `--ignore-user-config` | already fixed in this version; confirm you are on current `main` |
| Codex phase fails immediately | Codex is not logged in | run `codex` interactively once and sign in |
| Run stuck non-terminal after a reboot | the worker died with the machine | the next `duet_start` on that repo records it `FAILED`; start a new run |
| Finalize refuses a run that looks fine | the tree changed after validation, or the remote moved | read the returned evidence; re-run rather than overriding |
| `config.toml is group- or world-writable` | permissions | `chmod 600 ~/.config/agent-duet/config.toml` |

Operator commands:

```bash
agent-duet doctor                          # health report, never prints credentials
agent-duet gc --older-than 30              # dry run: list old terminal-run artifacts
agent-duet gc --older-than 30 --apply      # remove exactly what the dry run listed
```

`gc` only ever touches directories inside the state tree, and only for terminal runs.

---

## 11. Done means all of this is true

- `agent-duet --version`, `doctor`, `pytest`, `ruff`, and `mypy` all pass.
- Both clients show exactly five `agent_duet` tools.
- A disposable run reached `AWAITING_FINALIZE` with real Claude and real Codex.
- The run survived closing and reopening the client.
- Cancelling left no orphaned agent process.
- A finalize pushed a commit whose remote SHA matched the local SHA exactly.
- Run artifacts are `0700`/`0600` and contain no credentials.

Report which of these you verified and which you did not. Do not describe the system as
working, installed, or ready on the strength of anything you did not actually run.
