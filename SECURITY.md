# Security model

## Read this first: the children are not sandboxed

By deliberate operator decision, both child agents run with **full access to the
machine**:

- child Claude Code is launched with `--dangerously-skip-permissions`, so it can read,
  write, and execute anything the invoking user can;
- child Codex is launched with `--dangerously-bypass-approvals-and-sandbox`, so the
  reviewer has the same reach;
- `child_env_mode = "inherit"` hands both children the operator's entire environment,
  including every credential in it.

That is the shipped default because the workflow is meant to install dependencies, run
real test suites, and touch real tooling. It means **agent-duet is exactly as dangerous
as giving a stranger your shell**. Install it only where that is an acceptable trade, and
only on repositories you own.

Both knobs are configuration, not code. To tighten:

```toml
child_env_mode = "minimal"

[claude]
dangerously_skip_permissions = false
permission_mode = "acceptEdits"
allowed_tools = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git status *)"]

[codex]
sandbox_mode = "read-only"
write_policy = "fail"
```

With `sandbox_mode = "read-only"` the reviewer is mechanically prevented from writing, and
`write_policy = "fail"` turns any detected mutation into a failed run rather than a
warning.

---

# What is enforced regardless of those settings

Everything below is structural, not advisory. No configuration turns any of it off, and
every one of them has a test in `tests/`.

## Publishing is a separate, re-verified step

**Only the coordinator publishes.** `duet_start` contains no code that can commit, push,
deploy, change a remote, or rewrite history. Those operations exist only in
`duet_finalize`.

**Finalization re-verifies everything.** It refuses unless the run is exactly
`AWAITING_FINALIZE`, the branch matches, the normalized remote URL matches, HEAD is still
the validated base, and the combined diff fingerprint equals the one recorded at
validation time. Stale or altered work cannot be published.

**Only run-owned paths are staged.** `git add -A` is never used. The exact path list
recorded at validation time is staged, then re-read from the index; any unexpected staged
path aborts the commit and resets the index.

**Coordination artifacts never reach a commit.** Both handoff files are archived into the
private run directory and removed from the worktree before final validation, and
finalization refuses if either name appears in the commit set or still exists on disk.

## The reviewer cannot smuggle anything in

**The coordinator writes the critique, not the reviewer.** `GPT_CRITIQUE_FOR_CLAUDE.md` is
written by agent-duet, from the reviewer's captured final message, after that message
passes a structure check and a secret scan. The write is atomic (temp file, `fsync`,
`os.replace`) with mode `0600`.

## Nothing can call back into agent-duet

**No recursion.** Every child gets `AGENT_DUET_CHILD=1`; the server refuses to start and
every mutating tool refuses to run when that variable is present. That environment
variable is the guarantee — the CLI flags below are belt-and-braces on top of it.

Child Claude additionally gets `--strict-mcp-config` pointed at an empty MCP config plus
`--disallowedTools mcp__*`. Child Codex gets `--ignore-user-config`, which loads no user
config and therefore no agent_duet server. When that option is switched off in config,
`-c mcp_servers.agent_duet.enabled=false` is passed instead, to disable the server in the
config that *is* loaded. The two are mutually exclusive by construction: passing the
override alongside `--ignore-user-config` would create a partial table with no transport,
and codex rejects that outright.

## A run cannot collide with you, or with another run

**One writer per repository.** A non-blocking `flock` keyed by the canonical git *common*
directory — not the worktree path — so two MCP processes, or Claude and Codex
simultaneously, cannot run concurrent writers against one repository.

**A run owns a clean tree.** `direct_branch` mode, the default, edits the checkout in
place, so it refuses a dirty working tree and a detached HEAD rather than mixing its work
with yours. `review_branch` mode works in a private worktree on its own
`agent-duet/<short-run-id>` branch, so pre-existing changes are never swept in.

## Claims are never evidence

**Evidence beats claims.** Phase gates are process exit codes, parsed structured output,
git object ids, porcelain-v2 status, diff hashes, and validation exit codes. A model
saying it succeeded is not evidence and is never treated as one.

**Deployment is never assumed.** With no configured verifier, finalize reports
`NOT_CHECKED`. A verifier must emit JSON containing `deployed_sha`, `health`,
`checked_at`, and `target`, and the deployed SHA must equal the exact pushed commit. No
provider's success text is ever hard-coded.

## Execution is narrow by construction

**No shell, ever.** Every subprocess is an argv list through
`asyncio.create_subprocess_exec` or `subprocess.run`. `shell=True` appears nowhere.

**Dynamic content goes on stdin.** Task text, prompts, and commit messages are fed through
stdin so they never appear in `argv`, in `ps` output, or in a shell history.

**Stdout is protocol-only.** The stdio server writes nothing to stdout but JSON-RPC; all
diagnostics go to stderr through `logging`. A stray `print()` would corrupt the transport,
so there are none on the server path.

**Commands come from trusted config only.** Validation and deployment command vectors are
read from the operator's `config.toml`, keyed by canonical repository path, as TOML
arrays. A command supplied in task text, or read from a file inside the repository under
test, is never executed.

## Secrets are filtered on the way out

**Redaction is applied to everything that leaves.** Logs, evidence, error messages, and
the critique pass through a credential-shaped-content filter. The pre-commit scan refuses
to stage a file containing credential-shaped content, and files are size- and
binary-checked.

The rules key on the shape of a *value*, not on the name beside it, so a credential is
caught wherever it appears while ordinary code stays committable — a parser variable
called `tokens` is not a secret. When the scan does refuse, it names the rule that fired
and the line in the file, so the finding can be judged rather than guessed at.

---

## Reporting

This is a personal-infrastructure tool with no support commitment. If you find a hole in
the invariants above, the fix belongs in the test suite first: every one of them has a
test in `tests/`, and a regression should fail there before it is discussed anywhere else.
