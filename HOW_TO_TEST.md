# How to test agent-duet

You need three commands. No files to edit.

---

## The whole thing

```bash
cd Claude_Code_Codex_MCP_Coordinator/     # the folder you cloned
./setup.sh
```

Answer `y` when it offers to try a throwaway project. That is it — the script
installs everything, writes your config, registers agent-duet with both Claude Code
and Codex, installs the `/duet` command, and builds a tiny practice project.

It finishes by printing two lines to copy. They look like this:

```bash
cd ~/duet-demo/smoke && claude
```

and then, inside that session:

```
/duet Add a pure function add(a, b) to calc.py, with tests covering integers and floats
```

**Now wait 5 to 10 minutes.** That is normal. Three separate AI sessions run one after
another: Claude writes the code, Codex reviews it, Claude answers the review. Then your
tests run.

---

## What you should see

Status lines march through these stages:

```
QUEUED -> CLAUDE_IMPLEMENTING -> HANDOFF_VALIDATING -> CODEX_REVIEWING
       -> REVIEW_INTEGRITY_CHECK -> CLAUDE_RECONCILING -> FINAL_VALIDATING
       -> AWAITING_FINALIZE
```

Then it **stops** and shows you what happened. It asks before committing anything.

> If it commits without asking you, that is a bug. Tell me.

Say `Finalize this run.` if you are happy with it.

---

## When you are done

```bash
./setup.sh demo --clean
```

Deletes the practice project and removes it from your config. Nothing left behind.

---

## Using it on a real project

```bash
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
| Refuses to finalize | read the reason it gives; something changed after the tests ran |

---

## Optional: check its work yourself

Everything a run did is kept on disk. You do not have to trust the summary.

```bash
agent-duet runs                              # every run, newest first
agent-duet logs <run-id>                     # everything about one run
```

Three things worth confirming the first time:

**1. Your project was never touched while it worked.**

```bash
git -C ~/duet-demo/smoke log --oneline
git -C ~/duet-demo/smoke status --short
```

One commit, clean tree. All the work happened in a separate private copy.

**2. The run survives you closing the session.** Close Claude Code completely, open a
new session anywhere, and ask:

```
Call duet_status for run <run-id>.
```

Same run, still going. It does not belong to the session that started it.

**3. The reviewer really was read-only.** Look for `codex_readonly_verified: true` in the
evidence. agent-duet fingerprints the code before and after the review and compares — so
that is measured, not claimed.
