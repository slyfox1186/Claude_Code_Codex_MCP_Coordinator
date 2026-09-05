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

Without `-d`/`--directory`, setup offers a throwaway demo and then asks for a relative or
full project path. A plain folder is accepted: after explicit consent, setup initializes
local Git history from non-ignored files without adding a remote or uploading anything.

It finishes by printing two lines to copy.

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

That runs in a private copy and leaves the work on its own branch for you to merge.

---

## Is it working?

```bash
./setup.sh check
```

Tells you in plain English. If something is broken, `./setup.sh install` repairs it.

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
| `already active ... max_parallel_global is 1` | it names the run; `agent-duet cancel <run-id>` frees the slot |
| `refusing an in-place run ... dirty working tree` | commit or stash first, or ask for a review branch |
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
