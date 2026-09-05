# Try agent-duet in three commands

No files to edit. About 15 minutes, most of it waiting.

---

## 1. Install it

```bash
cd Claude_Code_Codex_MCP_Coordinator/     # the folder you cloned
./setup.sh -d /path/to/your/project
```

Use `--directory` instead of `-d` for the equivalent long form. Supplying either form
skips the demo and project-path questions. The script writes your config, registers
agent-duet with both Claude Code and Codex, installs the `/duet` command, and registers
the selected project.

For reliable polling in auto mode, it adds only the exact read-only rules
`mcp__agent_duet__duet_status` and `mcp__agent_duet__duet_wait` to Claude Code. The tools
that start, cancel, finalize, commit, or push are not preapproved.

Without `-d`/`--directory`, setup offers a throwaway demo and then asks for a relative or
full project path. A plain folder is accepted: setup automatically initializes local Git
history from non-ignored files without adding a remote or uploading anything.

It finishes by printing two lines to copy.

If setup updated an installation while either CLI was open, close and reopen that client
before using `/duet`. The old process cannot reload its MCP subprocess or command file.

## 2. Run it

```bash
cd ~/duet-demo/smoke && claude
```

Then, inside that session:

```
/duet Add a pure function add(a, b) to calc.py, with tests covering integers and floats
```

## 3. Wait

**5 to 10 minutes.** That is normal, not a hang. Three separate AI sessions run one after
another: Claude writes the code, Codex reviews it, Claude answers the review. Then your
tests run.

The phase marches along:

```
QUEUED -> CLAUDE_IMPLEMENTING -> HANDOFF_VALIDATING -> CODEX_REVIEWING
       -> REVIEW_INTEGRITY_CHECK -> CLAUDE_RECONCILING -> FINAL_VALIDATING
       -> AWAITING_FINALIZE
```

If the first final gate fails, Claude gets one focused repair pass and the full gate runs
again:

```
FINAL_VALIDATING -> CLAUDE_VALIDATION_REPAIRING -> FINAL_VALIDATING
```

A second failure stops the run with both attempts preserved. It is never valid to commit
or push that failed run directly; publishing is available only through `duet_finalize`
after `AWAITING_FINALIZE`.

Progress is trustworthy only when the returned status has the retained `run_id` and
`liveness.state` is `MODEL_ACTIVE`. `TRANSITIONING` means the worker is alive but the
expected model child was not verified at that instant. `WORKER_MISSING` is a failure, not
a reason to keep polling.
`CLEANUP_REQUIRED` means the run stopped but a worker or child process remains; retry
`duet_cancel` before starting another run.

You may start `/duet` in a second registered project while the first runs. The default
allows two projects at once but still refuses a second active run in the same repository.

Then it **stops** and tells you what it did.

> If it commits anything without asking you first, that is a bug. Tell me.

Say `Finalize this run.` if you are happy with it.

---

## Clean up

```bash
./setup.sh demo --clean
```

Deletes the practice project and removes it from your config. Nothing left behind.

---

## Using it on a real project

```bash
./setup.sh -d ~/code/my-project
./setup.sh --directory ~/code/my-project
./setup.sh add-repo ~/code/my-project
```

That one command allows the project, finds its test suite, and writes the config entry.
For Python tests it first asks to create a project-isolated environment and installs all
declared requirement files, the PEP 621 project package when present, and pytest. Say
`y` so the generated command can import the same dependencies as the project; say `n`
only when you intend to configure `validation_commands` yourself.
Then work normally:

```bash
cd ~/code/my-project
claude
```

```
/duet <describe what you want built>
```

Type `/duet` with nothing after it and it will ask you what you want.

To undo: `./setup.sh remove-repo ~/code/my-project`

**Where the work lands.** By default, on the branch you are already on — so finalizing
commits there. It needs a clean working tree, because it edits your checkout in place. If
you would rather it stayed out of your checkout entirely, say so when you start:

```
/duet <task> — put it on a review branch, I want to look first
```

That explicit request runs in a private copy and leaves the work on its own branch for
you to merge. A dirty tree alone never selects or suggests this mode.

---

## Is it working?

```bash
./setup.sh check
```

Tells you in plain English. If something is broken, `./setup.sh install` repairs it.
If only a registered project path is missing, setup remains healthy and tells you to
restore the folder or unregister it with `./setup.sh remove-repo /old/project/path`.

---

## If a run goes wrong

```bash
agent-duet logs
```

That prints everything about the most recent run — what it did, the exact commands it
executed, and why it stopped. It never prints passwords or tokens, so you can paste the
output to me as-is.

| What you saw | What to do |
|---|---|
| Fails right away at `CLAUDE_IMPLEMENTING` | run `claude` on its own once and sign in |
| Fails at `CODEX_REVIEWING` | run `codex` on its own once and sign in |
| `not below an allowed_repo_roots entry` | run `./setup.sh add-repo <the project>` |
| `already active ... max_parallel_global is N` | all global slots are occupied; wait, finalize, cancel, or raise the configured limit |
| `refusing an in-place run ... dirty working tree` | commit or stash first, then retry on the same branch |
| `Denied by auto mode classifier` while polling | run `./setup.sh install` from the Agent Duet checkout, or add `mcp__agent_duet__duet_status` and `mcp__agent_duet__duet_wait` through Claude Code's `/permissions` |
| Validation fails twice | inspect both attempts with `agent-duet logs <run-id>`; do not publish the failed tree |
| Refuses to finalize | read the reason it gives; something changed after the tests ran |

---

## Optional: check its work yourself

Everything a run did is kept on disk. You do not have to trust the summary.

```bash
agent-duet runs                              # every run, newest first
agent-duet logs <run-id>                     # everything about one run
```

Two things worth confirming the first time:

**1. The run survives you closing the session.** Close Claude Code completely, open a new
session anywhere, and ask:

```
Call duet_status for run <run-id>.
```

Same run, still going. It does not belong to the session that started it.

**2. The reviewer really was read-only.** Look for `codex_readonly_verified: true` in the
evidence. agent-duet fingerprints the code before and after the review and compares — so
that is measured, not claimed.
