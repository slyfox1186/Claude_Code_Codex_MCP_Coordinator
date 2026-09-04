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

## 5. Configure and register — one command

```bash
cd "$HOME/src/agent-duet"
./setup.sh install
```

That single command does all of the following, and refuses to write any file it cannot
parse back:

| It does this | Instead of you doing this |
|---|---|
| Writes `$HOME/.config/agent-duet/config.toml` from `config.example.toml`, with the real paths filled in | Copying the example and replacing four `REPLACE_ME` placeholders by hand |
| `chmod 700` on the config and state directories, `600` on the config | Remembering to, and being refused at load time if you forget |
| `claude mcp add-json --scope user` with a 330 000 ms timeout | Hand-writing escaped JSON on the command line |
| `codex mcp add`, then extends that table with `startup_timeout_sec`, `tool_timeout_sec = 330`, and the five-tool allowlist | Hand-editing `$HOME/.codex/config.toml` and not accidentally creating a second table |
| Installs `commands/duet.md` into `~/.claude/commands/` and `~/.codex/prompts/` | Copying two files |

It backs up anything it overwrites to `<name>.duet-backup`, never uses `sudo`, and is safe
to run repeatedly.

The 330 s timeouts are not decoration: `duet_wait` deliberately blocks for up to 300 s, and
a client timeout below that kills a healthy call.

**Checks**

```bash
./setup.sh check
```

Every line is a `✓`, ending in `Everything works.` It verifies `agent-duet doctor`, that
Claude Code reports `Connected`, that Codex reports all five tools, and that both `/duet`
files exist.

Then start each CLI interactively and run `/mcp`. Both must show `agent_duet` with exactly
five tools: `duet_start`, `duet_status`, `duet_wait`, `duet_cancel`, `duet_finalize`.

### Allowing a repository

The coordinator will only work on a repository that is named in the config, and only if it
sits **strictly below** an `allowed_repo_roots` entry.

```bash
./setup.sh add-repo /path/to/project
```

This allows the project's parent directory, detects its test suite (pytest, `npm test`,
`cargo test`, or `go test`), and writes the `[[repositories]]` entry between markers so
`./setup.sh remove-repo` can take it back out cleanly. It refuses a project sitting
directly in `$HOME` and tells you to move it one level down, because the loader will not
accept a home directory as a root.

Those `validation_commands` are the coordinator's own authoritative check, run after both
agents finish. They are command **vectors**, never shell strings, and they are read only
from this config file — never from task text or from a file inside the repository under
test. Edit them by hand whenever the detected default is not what you want.

---

## 6. Prove it end to end on a disposable repository

Do **not** make the first run against anything you care about.

```bash
cd "$HOME/src/agent-duet"
./setup.sh demo
```

That builds a throwaway git repository at `$HOME/duet-demo/smoke` with a `multiply`
function and a passing test, gives it a local bare remote so push can be verified for
real, registers it, and prints the two lines to copy. `./setup.sh demo --clean` removes
the directory, the `[[repositories]]` entry, and the allowed root it added.

Then start an interactive Claude Code session in that directory and say:

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

## 7. Finalize, only when you mean it

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

## 8. Two-machine rules

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

## 9. Troubleshooting

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

## 10. Done means all of this is true

- `agent-duet --version`, `doctor`, `pytest`, `ruff`, and `mypy` all pass.
- Both clients show exactly five `agent_duet` tools.
- A disposable run reached `AWAITING_FINALIZE` with real Claude and real Codex.
- The run survived closing and reopening the client.
- Cancelling left no orphaned agent process.
- A finalize pushed a commit whose remote SHA matched the local SHA exactly.
- Run artifacts are `0700`/`0600` and contain no credentials.

Report which of these you verified and which you did not. Do not describe the system as
working, installed, or ready on the strength of anything you did not actually run.
