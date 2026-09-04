# agent-duet

A local stdio **MCP coordinator** that runs one disciplined workflow on demand from
either Claude Code or the Codex CLI:

1. a fresh **Claude Code** process implements the task and writes a handoff;
2. a fresh **Codex** process reviews it independently and returns a critique;
3. a fresh **Claude Code** process adjudicates that critique, fixes what is justified,
   and revalidates;
4. the run stops and waits for a human;
5. a **separate** tool call commits, pushes, and optionally verifies deployment.

The point is separation of duties without copy/paste. The implementer never reviews its
own work, the reviewer never edits, and neither one publishes.

## What it does not do

`duet_start` cannot commit, push, deploy, change remotes, or rewrite history — not by
policy text, but because that code does not exist on that path. Publishing lives only in
`duet_finalize`, which re-verifies the branch, the remote URL, and the exact diff
fingerprint that was validated before it will touch anything.

## Requirements

- Linux, Python 3.13+
- `git`
- Claude Code CLI (tested against 2.1.236)
- Codex CLI (tested against 0.153.2)

Dependencies are pinned in `pyproject.toml` and `requirements-lock.txt`. There is no
virtualenv: the package installs into the Python interpreter you name below.

## Install

```bash
PY=/home/YOU/miniconda3/bin/python          # your interpreter

git clone <your private repo url> "$HOME/src/agent-duet"
cd "$HOME/src/agent-duet"
"$PY" -m pip install -r requirements-lock.txt
"$PY" -m pip install -e ".[dev]"
"$PY" -m pytest
```

Then create the config:

```bash
mkdir -p "$HOME/.config/agent-duet" "$HOME/.local/state/agent-duet"
cp config.example.toml "$HOME/.config/agent-duet/config.toml"
$EDITOR "$HOME/.config/agent-duet/config.toml"   # replace every REPLACE_ME
chmod 700 "$HOME/.config/agent-duet" "$HOME/.local/state/agent-duet"
chmod 600 "$HOME/.config/agent-duet/config.toml"
agent-duet doctor
```

The config file must be a regular file you own, and must not be group- or
world-writable: it names the executables to run and the exact command vectors the
coordinator will execute, so write access to it is equivalent to code execution.
Loading refuses otherwise.

`doctor` resolves both CLI paths, prints their versions, checks the state directory
permissions and the SQLite database, and never prints a credential.

## Register with both CLIs

Use the absolute path of the installed console script (`command -v agent-duet`):

```bash
DUET_BIN="$(command -v agent-duet)"

claude mcp add-json --scope user agent_duet \
  "{\"type\":\"stdio\",\"command\":\"$DUET_BIN\",\"args\":[],\"env\":{},\"timeout\":330000}"

codex mcp add agent_duet -- "$DUET_BIN"
```

Then run `/mcp` inside each client and confirm exactly five tools appear.

## Daily use

Ask either CLI, in its own words:

```
Use agent_duet for this repository. Start one standard Claude->Codex->Claude run for the
task below, wait until it reaches AWAITING_FINALIZE or a terminal failure, and summarize
the evidence. Do not finalize until you ask me and I approve.

Task:
<your task>

Acceptance criteria:
- <criterion 1>
```

When you are satisfied with the evidence:

```
Finalize run <run-id> only if its validated diff is unchanged. Expected branch: <branch>.
Expected remote: origin. Expected remote URL: <exact URL>. Commit message: <message>.
Push it, verify the remote ref equals the exact commit, and report exact evidence.
```

## The tools

| Tool | What it does | Publishes? |
|---|---|---|
| `duet_start` | Validates the repo, creates the run, spawns a detached worker, returns a `run_id` in seconds | No |
| `duet_status` | Durable phase, timestamps, evidence, next action | No |
| `duet_wait` | The same shape, after waiting up to 300s for a change | No |
| `duet_cancel` | Sets the cancel flag and reaps the worker's process group | No |
| `duet_finalize` | Commits, pushes, verifies the remote ref, runs a deployment verifier | **Yes** |

The run survives the client that started it: the worker is detached, so you can close
the terminal, reopen either CLI, and `duet_status <run-id>` picks up where it left off.

If the *worker itself* dies, that is terminal. The next `duet_start` on that repository
records the run as `FAILED` with its evidence preserved; there is no phase-aware resume,
and a half-finished run is never silently continued.

## Phases

```
QUEUED -> CLAUDE_IMPLEMENTING -> HANDOFF_VALIDATING -> CODEX_REVIEWING
       -> REVIEW_INTEGRITY_CHECK -> CLAUDE_RECONCILING -> FINAL_VALIDATING
       -> AWAITING_FINALIZE -> FINALIZING -> COMPLETE
```

`FAILED`, `CANCELLED`, and `COMPLETE` are terminal. Every transition is written to
SQLite with a timestamp and a reason before the work that follows it happens.

## Where things live

```
~/.config/agent-duet/config.toml               0600
~/.local/state/agent-duet/state.sqlite3        0600, WAL
~/.local/state/agent-duet/runs/<run-id>/       0700   prompts, logs, artifacts, manifests
~/.local/state/agent-duet/worktrees/...        0700   the per-run worktree
~/.local/state/agent-duet/locks/               0700   one flock per repository
```

Keep the state directory off NFS, Dropbox, and Syncthing. Two machines must never share
it; they coordinate through git remotes, not through this database.

## Operator commands

```bash
agent-duet doctor              # health report
agent-duet gc --older-than 30  # dry run: list old terminal-run artifacts
agent-duet gc --older-than 30 --apply
```

`gc` only ever touches directories inside the state tree, and only for terminal runs.

## Security posture

Child agents run with **full, unsandboxed machine access** by deliberate operator
decision. Read [SECURITY.md](SECURITY.md) before installing this anywhere you would not
hand the same access to a person.
