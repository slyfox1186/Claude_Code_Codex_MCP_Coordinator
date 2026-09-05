# agent-duet

**Have Claude write the code, have Codex review it, then have Claude answer the review —
automatically, in one command, with nothing committed until you say so.**

You type `/duet <what you want built>`. Three separate AI sessions run one after another,
your tests run, and then it stops and shows you what happened. You decide whether it ships.

---

## Why bother

Asking one model to review its own work does not really work — it agrees with itself. The
usual fix is to copy the diff into a second tool by hand, paste the critique back, and
keep track of which round you are on. That is the whole job this does for you.

Three rules make it worth the trouble, and none of them is a promise in a prompt — each
one is enforced by code:

- **The implementer never reviews its own work.** The reviewer is a different CLI, in a
  different process, with no memory of writing the code.
- **The reviewer cannot edit anything.** agent-duet fingerprints the repository before and
  after the review and compares. If the reviewer touched a file, you are told.
- **Neither one can publish.** Committing and pushing live in a separate tool call that
  only runs after you approve.

---

## How a run works

```
   you ──▶ /duet "add retry-with-backoff to the HTTP client"
                    │
      ┌─────────────▼─────────────┐
      │ 1. Claude Code implements │   a fresh process, no history
      └─────────────┬─────────────┘
      ┌─────────────▼─────────────┐
      │ 2. Codex reviews          │   read-only, verified read-only
      └─────────────┬─────────────┘
      ┌─────────────▼─────────────┐
      │ 3. Claude Code reconciles │   fixes what is justified, argues what is not
      └─────────────┬─────────────┘
      ┌─────────────▼─────────────┐
      │ 4. your tests run         │
      └─────────────┬─────────────┘
                    ▼
              it STOPS and reports  ──▶  you approve  ──▶  commit + push
```

Expect **5 to 20 minutes**. Three real AI sessions run end to end. That is normal, and the
work keeps going even if you close your terminal.

---

## Install

```bash
git clone https://github.com/slyfox1186/Claude_Code_Codex_MCP_Coordinator.git
cd Claude_Code_Codex_MCP_Coordinator
./setup.sh
```

That is the whole installation. Setup explains what it needs and asks for consent before
creating an environment or installing a missing provider CLI.

- If Conda is detected, it creates a dedicated environment named `agent-duet`. Setup never installs Conda or changes `base` or another environment.
- Without Conda, it uses the default Python 3.13+ only to create a private environment;
  packages are not installed into system Python.
- If Claude Code or Codex is missing, it offers the official installer. It also offers
  sign-in and a throwaway demo.
- Answer `n` to the demo and setup asks for your real repository's relative or full path.

Requirements: Linux, Git, and Python 3.13+. `curl` or `wget` is needed only if a provider
CLI must be downloaded. See **[INSTALL.md](INSTALL.md)** for the short guide and read
**[SECURITY.md](SECURITY.md)** before use.

Setup never uses `sudo`. It validates generated configuration, backs up files it replaces
to `<name>.duet-backup`, and is safe to rerun.

---

## Use it

Point it at a project once:

```bash
./setup.sh add-repo ~/code/my-project
```

Then work normally, in either CLI:

```
/duet Add retry-with-backoff to the HTTP client and cover it with tests
```

Type `/duet` with nothing after it and it will work out the task from your conversation,
or ask you if there is nothing to work from. Either way it confirms the acceptance
criteria before spending your time.

When it finishes it prints what it did and waits. Say **"finalize"** and it commits and
pushes. Say nothing and nothing happens.

### Other setup commands

```bash
./setup.sh check                          # is everything working?
./setup.sh add-repo ~/code/project        # let it work on a project
./setup.sh remove-repo ~/code/project
./setup.sh demo                           # a throwaway project to try it on
./setup.sh demo --clean
./setup.sh uninstall
```

`add-repo` allows the project's parent directory, detects its test suite (pytest,
`npm test`, `cargo test`, `go test`), and writes the config entry between markers so
`remove-repo` takes it back out cleanly.

---

## Where the work ends up

By default a run works **on the branch you are already on**, so finalizing commits there.
If you are on `main`, it commits to `main`. That is what most people mean by "make this
change", and it needs a clean working tree, because the run edits your checkout in place.

If you would rather look before it touches your branch, ask for a review branch:

```
/duet <task> — put it on a review branch, I want to look first
```

That runs in a private worktree instead, leaving your checkout completely untouched, and
lands the work on its own `agent-duet/<id>` branch for you to merge. It is also the mode
to use when your tree is dirty and you do not want to stash.

Set the default for the machine with `default_delivery_mode` in `[git]` in your config.

---

## Checking its work yourself

Everything a run did is kept on disk. You never have to trust the summary.

```bash
agent-duet runs                # every run, newest first
agent-duet logs <run-id>       # everything about one run
```

`logs` prints the whole story: every command it ran, every argument, every phase
transition with a timestamp, and why it stopped. It never prints passwords or tokens, so
you can paste it to someone as-is.

Worth confirming once, the first time:

- **The reviewer really was read-only.** Look for `codex_readonly_verified: true`. That is
  measured — a fingerprint before and after — not claimed.
- **The run outlives your session.** Close the CLI completely, open a new one anywhere,
  and ask for `duet_status` on the run id. Same run, still going.

---

## The tools

`/duet` calls these for you. You can also just ask either CLI in plain words.

| Tool | What it does | Publishes? |
|---|---|---|
| `duet_start` | Validates the repo, creates the run, spawns a detached worker, returns a `run_id` in seconds | No |
| `duet_status` | Durable phase, timestamps, evidence, next action | No |
| `duet_wait` | The same, after waiting up to 300 s for something to change | No |
| `duet_cancel` | Sets the cancel flag and reaps the worker's process group | No |
| `duet_finalize` | Commits, pushes, verifies the remote ref, runs a deployment verifier | **Yes** |

`duet_start` **cannot** commit, push, deploy, change remotes, or rewrite history — not
because a prompt forbids it, but because that code does not exist on that path.
Publishing lives only in `duet_finalize`, which re-verifies the branch, the remote URL,
and the exact diff fingerprint that was validated before it will touch anything.

---

## Phases

```
QUEUED -> CLAUDE_IMPLEMENTING -> HANDOFF_VALIDATING -> CODEX_REVIEWING
       -> REVIEW_INTEGRITY_CHECK -> CLAUDE_RECONCILING -> FINAL_VALIDATING
       -> AWAITING_FINALIZE -> FINALIZING -> COMPLETE
```

`FAILED`, `CANCELLED`, and `COMPLETE` are terminal. Every transition is written to SQLite
with a timestamp and a reason *before* the work that follows it happens.

The run survives the client that started it — the worker is detached, so you can close the
terminal, reopen either CLI, and pick up where you left off.

If the *worker itself* dies, that is terminal. The next `duet_start` on that repository
records the run as `FAILED` with its evidence preserved. There is no phase-aware resume,
and a half-finished run is never silently continued.

---

## When something goes wrong

```bash
agent-duet logs        # the most recent run, in full
```

| What you saw | What to do |
|---|---|
| Fails immediately at `CLAUDE_IMPLEMENTING` | run `claude` on its own once and sign in |
| Fails at `CODEX_REVIEWING` | run `codex` on its own once and sign in |
| `not below an allowed_repo_roots entry` | `./setup.sh add-repo <the project>` |
| `already active ... max_parallel_global is 1` | it names the run; `agent-duet cancel <run-id>` frees the slot |
| `refusing an in-place run ... dirty working tree` | commit or stash, or ask for a review branch |
| Refuses to finalize | read the reason; something changed after the tests ran |

---

## Operator commands

```bash
agent-duet doctor              # health report
agent-duet runs                # every run, newest first
agent-duet logs [run-id]       # everything about one run (default: the most recent)
agent-duet cancel <run-id>     # clear an unfinished run and free its slot
agent-duet gc --older-than 30  # dry run: list what old terminal runs left behind
agent-duet gc --older-than 30 --apply
```

`cancel` accepts an id prefix. It exists because a run parked at `AWAITING_FINALIZE` has
no live worker and is never reaped — it is waiting for a person — yet it still counts as
active, so with the default `max_parallel_global = 1` it blocks every new run until
someone finalizes or cancels it.

`gc` forgets a terminal run completely: its artifact directories, git's worktree
registration in the real repository, and its row in the listing. It never deletes a run's
branch — that holds the work — so it reports the branches it orphans instead. It only ever
touches directories inside the state tree, and only for terminal runs.

---

## Where things live

```
~/.config/agent-duet/config.toml               0600
~/.local/state/agent-duet/state.sqlite3        0600, WAL
~/.local/state/agent-duet/runs/<run-id>/       0700   prompts, logs, artifacts, manifests
~/.local/state/agent-duet/worktrees/...        0700   private worktrees (review-branch runs)
~/.local/state/agent-duet/locks/               0700   one flock per repository
```

Keep the state directory off NFS, Dropbox, and Syncthing. Two machines must never share
it; they coordinate through git remotes, not through this database.

The config file must be a regular file you own, and must not be group- or world-writable:
it names the executables to run and the exact command vectors the coordinator will
execute, so write access to it is equivalent to code execution. Loading refuses otherwise.

---

## Installing by hand

Everything `setup.sh` does is ordinary configuration; nothing is hidden. Copy
`config.example.toml` to `~/.config/agent-duet/config.toml`, replace every `REPLACE_ME`,
`chmod 700` the config and state directories and `600` the config file, then run
`agent-duet doctor`. Register the server:

```bash
claude mcp add-json --scope user agent_duet \
  '{"type":"stdio","command":"<path>","args":[],"env":{},"timeout":330000}'
codex mcp add agent_duet -- <path>
```

Then add `tool_timeout_sec = 330` and `enabled_tools` to the `[mcp_servers.agent_duet]`
table Codex wrote — `duet_wait` needs that timeout — and copy `commands/duet.md` into
`~/.claude/commands/` and `~/.codex/prompts/`.

---

## Security posture

Child agents run with **full, unsandboxed machine access** by deliberate operator
decision. Read **[SECURITY.md](SECURITY.md)** before installing this anywhere you would
not hand the same access to a person.

---

## More

- **[INSTALL.md](INSTALL.md)** — the short server installation guide
- **[HOW_TO_TEST.md](HOW_TO_TEST.md)** — try it in three commands
- **[SECURITY.md](SECURITY.md)** — the trust model and every guard, in detail
- **[HOW_TO_BUILD_THIS.md](HOW_TO_BUILD_THIS.md)** — installing it on another machine,
  step by step with a check after every step. Written to hand straight to Claude Code on
  the target PC.
