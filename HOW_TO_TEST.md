# How to live test agent-duet

Ten minutes, one disposable repository, nothing of yours at risk. Every step is a single
copy-paste block followed by what you should see.

A full run takes **5 to 10 minutes** of real model time. That is normal — three separate
agent sessions run back to back.

---

## 1. Check the install

```bash
agent-duet doctor
```

**Expect** the last line to be `result: OK`, and both CLI versions printed.

If it is not OK, fix that first — nothing below will work.

---

## 2. Check both CLIs can see the tools

```bash
claude mcp get agent_duet
codex mcp get agent_duet
```

**Expect** `Status: ✔ Connected` from Claude, and `enabled_tools` listing all five from
Codex: `duet_start`, `duet_status`, `duet_wait`, `duet_cancel`, `duet_finalize`.

---

## 3. Build a throwaway repository

```bash
rm -rf /home/jman/tmp/duet-smoke /home/jman/tmp/duet-smoke-remote.git
mkdir -p /home/jman/tmp/duet-smoke
cd /home/jman/tmp/duet-smoke
git init -q -b main .
git config user.email jeff@coastaltech.group
git config user.name slyfox1186
printf '__pycache__/\n*.pyc\n' > .gitignore
printf '#!/usr/bin/env python3\n"""A tiny module."""\n\n\ndef multiply(a, b):\n    """Return a * b."""\n    return a * b\n' > calc.py
printf '#!/usr/bin/env python3\n"""Tests."""\n\nfrom calc import multiply\n\n\ndef test_multiply():\n    assert multiply(3, 4) == 12\n' > test_calc.py
git add -A && git commit -q -m "initial"
git init -q --bare -b main /home/jman/tmp/duet-smoke-remote.git
git remote add origin /home/jman/tmp/duet-smoke-remote.git
git push -q origin main
git rev-parse HEAD
```

**Expect** a 40-character commit SHA. Write it down — that is the baseline.

The `.gitignore` matters: without it, `pytest` bytecode shows up as untracked files.
agent-duet excludes anything its own validation commands produce, but a real project
should ignore them anyway.

---

## 4. Tell agent-duet about the repository

Append to `/home/jman/.config/agent-duet/config.toml`:

```toml
[[repositories]]
path = "/home/jman/tmp/duet-smoke"
validation_commands = [
  ["/home/jman/miniconda3/bin/python", "-m", "pytest", "-q"],
]
validation_timeout_seconds = 600
```

Then:

```bash
agent-duet doctor
```

**Expect** a line reading
`repo /home/jman/tmp/duet-smoke: present, 1 validation command(s)` and `result: OK`.

That validation command is the coordinator's own check. It runs after both agents finish,
and it is the authority — not anything a model claims.

---

## 5. Run it

```bash
cd /home/jman/tmp/duet-smoke
claude
```

Inside the session:

```
/duet Add a pure function named add(a, b) to calc.py that returns a + b, and tests covering integers and floats in test_calc.py. Keep multiply and its test working.
```

Or just type `/duet` with nothing after it and let it ask you what to build.

**Expect**, over the next several minutes, the phases in this order:

```
QUEUED -> CLAUDE_IMPLEMENTING -> HANDOFF_VALIDATING -> CODEX_REVIEWING
       -> REVIEW_INTEGRITY_CHECK -> CLAUDE_RECONCILING -> FINAL_VALIDATING
       -> AWAITING_FINALIZE
```

Then it stops, summarizes the evidence, and asks before finalizing. **It should not
finalize on its own.** If it does, that is a bug — tell me.

---

## 6. Check the evidence yourself

Take the run id from the session:

```bash
RID=<paste-the-run-id>
ls -la /home/jman/.local/state/agent-duet/runs/$RID/
cat  /home/jman/.local/state/agent-duet/runs/$RID/validation-manifest.json
cat  /home/jman/.local/state/agent-duet/runs/$RID/artifacts/GPT_CRITIQUE_FOR_CLAUDE.md
```

**Expect**

- the run directory is `drwx------`, the files inside `-rw-------`;
- the manifest shows `pytest -q` with `"exit_code": 0` and `"passed": true`;
- the archived critique is Codex's real review;
- **neither** `CLAUDE_CRITIQUE_REQUEST.md` nor `GPT_CRITIQUE_FOR_CLAUDE.md` is left in the
  worktree — they were archived here and removed so they cannot be committed.

And the original repository is untouched:

```bash
git -C /home/jman/tmp/duet-smoke log --oneline
git -C /home/jman/tmp/duet-smoke status --short
```

**Expect** the same single baseline commit and a clean tree. All the work happened in a
private worktree on an `agent-duet/<id>` branch.

---

## 7. Prove the run outlives the client

Close the Claude Code session completely. Open a new one anywhere:

```
Call duet_status for run <run-id>.
```

**Expect** the same run, same phase. The worker is detached; it does not belong to the
session that started it.

---

## 8. Finalize

Back in a session, once you have read the evidence:

```
Finalize this run. Push it and verify the remote ref equals the exact commit.
```

**Expect** a local SHA and a remote SHA that are identical. Check it yourself:

```bash
git ls-remote /home/jman/tmp/duet-smoke-remote.git
```

**Expect** the same SHA the tool reported, and deployment reported as `NOT_CHECKED`
(there is no verifier configured — that is correct, and it should never claim otherwise).

---

## 9. Optional: prove the safety rails

**Cancellation reaps the child.** Start another run, and while a phase is mid-flight say
`Cancel this run.` Then:

```bash
ps -ef | grep -E "claude|codex" | grep -v grep
```

**Expect** no leftover agent process. Each child runs in its own process group, so this
is worth checking rather than assuming.

**Finalize refuses stale work.** After a run reaches `AWAITING_FINALIZE`, edit one of the
files it changed, then ask it to finalize.

**Expect** a refusal mentioning the content changed after validation, and nothing pushed.

**Codex cannot smuggle in changes.** Look at `codex_readonly_verified` in the evidence.
It should be `true` — the coordinator fingerprints the tree before and after the review
and compares.

---

## 10. Run it from Codex too

```bash
cd /home/jman/tmp/duet-smoke
codex
```

```
/duet Add a subtract(a, b) function to calc.py with tests.
```

**Expect** the same workflow. Either CLI can start it; the roles inside never change —
Claude always implements, Codex always reviews.

---

## Clean up

```bash
rm -rf /home/jman/tmp/duet-smoke /home/jman/tmp/duet-smoke-remote.git
agent-duet gc --older-than 0            # lists what it would remove
agent-duet gc --older-than 0 --apply    # removes exactly that
```

Then delete the `[[repositories]]` block you added in step 4.

---

## If something goes wrong

Everything a run did is on disk. Start here:

```bash
RID=<run-id>
D=/home/jman/.local/state/agent-duet/runs/$RID
cat $D/worker.stderr.log                 # what the coordinator was doing
cat $D/phase1-claude.argv.json           # the exact command line used
tail -40 $D/phase2-codex.stderr.log      # why the reviewer failed, if it did
```

| What you see | What it means |
|---|---|
| Run fails instantly at `CLAUDE_IMPLEMENTING` | Claude is not logged in, or the executable path in the config is wrong |
| Run fails at `CODEX_REVIEWING` | run `codex` interactively once and sign in |
| `not below an allowed_repo_roots entry` | add the parent directory to `allowed_repo_roots` |
| Tool call times out around 60s | the client tool timeout is under 330s |
| `a run is already active for ...` | cancel or finish the previous run on that repo |
| Finalize refuses and looks wrong | read the returned fingerprints; re-run rather than forcing it |

The run directory never contains credentials — logs and evidence are passed through a
redactor on the way out. You can paste them to me as-is.
