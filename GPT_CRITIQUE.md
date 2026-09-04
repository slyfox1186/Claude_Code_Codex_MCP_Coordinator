# Codex critique for Claude Code

## Verdict

Do not commit, push, install, or register this project yet. The implementation is not
ready: its integration suite fails, the main MCP status path is broken by an internal
schema mismatch, multiple security invariants from `BUILD_SPEC.md` were deliberately
reversed, cancellation can orphan the active child agent, and the requested GitHub and
machine setup has not been completed.

Treat `BUILD_SPEC.md` as authoritative. The user's willingness to use a private GitHub
repository or a GitHub credential did **not** authorize weakening the coordinator's
child isolation, reviewer read-only boundary, environment handling, or fail-closed
behavior. Do not preserve those deviations as “operator choice.”

## Confirmed release blockers

### 1. The shipped security posture contradicts the non-negotiable build specification

Severity: **critical**

Evidence:

- `src/agent_duet/config.py` defaults Claude to `dangerously_skip_permissions=True`,
  Codex to `sandbox_mode="bypass"`, Codex mutations to `write_policy="warn"`, and child
  environments to `child_env_mode="inherit"`.
- `config.example.toml` ships the same unsafe values.
- `src/agent_duet/runners.py` consequently emits
  `--dangerously-skip-permissions` and
  `--dangerously-bypass-approvals-and-sandbox`.
- `src/agent_duet/worker.py::_apply_review_integrity_policy` allows a run to continue
  after Codex changes the repository when `write_policy="warn"`.
- `README.md`, `SECURITY.md`, and tests describe this reversal as intentional.

This violates the guide's explicit invariants that Codex be OS-enforced read-only, that
only Claude edit implementation files, that any pre/post review change fail the run,
and that children receive a minimal environment.

Required correction:

- Remove the bypass modes and `write_policy="warn"` from the supported production
  contract; do not merely change the example defaults while retaining an easy bypass.
- Always invoke Codex with `--sandbox read-only`, `--ephemeral`,
  `--ignore-user-config`, JSON output, stdin prompt, and the coordinator MCP disabled.
- Fail the run on **any** review-phase fingerprint difference.
- Restore Claude's constrained `acceptEdits` posture and trusted exact allowed-tool
  list. Never grant broad Bash by default.
- Make the minimal explicit environment the only normal child environment. Preserve
  only variables demonstrated to be necessary for the installed CLI authentication,
  locale, Git, and SSH behavior. Never inherit token-shaped environment variables.
- Rewrite the documentation and tests so they assert the required posture instead of
  defending the unsafe one.

Validation:

- Add tests that reject unsafe configuration values and assert the exact production
  argv/environment.
- Add an integration test in which fake Codex attempts a write and the write itself is
  denied; also prove the complete pre/post Git fingerprint is unchanged.

### 2. `duet_start`/`duet_status` cannot serialize their own stored evidence

Severity: **critical**

Evidence:

- `src/agent_duet/server.py::create_run` stores `start_remotes` in `evidence`.
- Later phases also store `critique_redacted` and `changed_path_count`.
- `src/agent_duet/models.py::Evidence` uses `extra="forbid"` but declares none of those
  fields.
- `src/agent_duet/state.py::RunRecord.to_status` validates the stored mapping through
  `Evidence.model_validate`, so the normal MCP return path raises a Pydantic validation
  error.
- The current integration failure reproduces all three rejected keys. Because
  `duet_start` calls `updated.to_status()` after creating the row, `start_remotes` alone
  is enough to break the very first real tool response.

Required correction:

- Define every persisted/public evidence field in one authoritative schema, or split
  private run metadata from the bounded MCP-facing evidence model.
- Validate evidence at every database write, not only when it is returned.
- Add direct tests for all five decorated MCP functions, especially a real
  `duet_start` return followed by `duet_status` and `duet_wait`.

### 3. Cancellation can leave Claude or Codex running after the run is marked cancelled

Severity: **critical**

Evidence:

- The detached worker starts a new session in
  `src/agent_duet/process_guard.py::spawn_detached_worker`.
- Each active Claude/Codex child starts **another** new session in
  `src/agent_duet/runners.py::run_child` (`start_new_session=True`).
- `duet_cancel` sets the cancellation flag and immediately signals only the worker's
  recorded process group. The active child is in a different group, so it is not killed;
  killing the worker also prevents its cooperative poller from cleaning that child up.
- The existing “whole group” test uses a grandchild that remains in its parent's group,
  so it does not exercise the architecture actually used by `run_child`.

Required correction:

- Give cancellation a durable identity for the currently active child PID/PGID/start
  ticks, and terminate that verified group before or while reaping the worker; or keep
  agent children in a hierarchy the worker-group signal actually reaches.
- Do not transition to `CANCELLED` until the active agent process group is confirmed
  gone. If cleanup partly fails, preserve that fact in the terminal evidence.
- Add a process-level test that launches the real worker topology, cancels during a
  sleeping child, and proves both worker and agent descendants are gone.

### 4. The captured baseline is lost in a start/worker race

Severity: **high**

Evidence:

- `create_run` records the validated starting `HEAD` as `base_sha`, then releases its
  probe lock before the detached worker acquires the long-lived lock.
- `Worker._prepare_worktree` re-inspects the repository and creates the worktree from
  the **current** `info.head_sha`, then overwrites `base_sha` and `current_sha` in the
  database. Direct-branch mode does the same.
- If HEAD moves between start validation and worker preparation, the run silently
  changes its baseline. This defeats `expected_base_ref` and makes the returned start
  evidence untrustworthy.

Required correction:

- Revalidate under the worker-held lock that canonical repo, Git common directory,
  HEAD, branch/mode preconditions, and recorded remotes still match the durable start
  record.
- Always create the review worktree from `record.base_sha`; never replace the captured
  base with a later observation.
- Make start admission atomic enough that two callers cannot both pass the active-run
  check and lock probe before either worker owns the repository. Enforce the configured
  `max_parallel_global`, which is currently parsed but unused.
- Add deterministic TOCTOU and simultaneous-start tests across processes.

### 5. Remote integrity is not preserved through reconciliation/finalization

Severity: **high**

Evidence:

- Remotes are checked after phase 1, but phase 3 can change them.
- Final validation does not compare current remotes with the start record.
- `_finalize_locked` compares the current remote only with the caller-supplied URL, not
  with the normalized URL stored at run creation. A changed remote can therefore be
  accepted if the finalization request repeats the changed value.

Required correction:

- Store normalized fetch/push URLs at start and require exact equality after every
  write-capable phase and again under the finalization lock.
- Require current remote name/URL to match both the explicit finalize request **and**
  the immutable run record.
- Test phase-3 remote replacement, fetch/push URL divergence, and a finalize request
  that attempts to bless the replacement.

### 6. The validated diff hash is not content-safe for binary files or symlinks

Severity: **high**

Evidence:

- `src/agent_duet/git_guard.py::combined_diff_sha256` hashes ordinary textual
  `git diff --no-color` output for tracked files. Different binary contents can produce
  the same “Binary files differ” patch text, allowing content to change after validation
  without changing the fingerprint.
- For untracked symlinks it calls `Path.read_bytes()`, which follows the link rather than
  hashing the Git-staged link target text and file mode. Retargeting a symlink to equal
  content can therefore preserve the validation hash while changing the object Git will
  commit. Special files can also make direct reads unsafe or blocking.

Required correction:

- Fingerprint the exact Git object content and modes that would be committed. A robust
  approach is an isolated temporary index populated only with run-owned paths, followed
  by `git write-tree`; record the resulting tree ID at validation and require the exact
  same tree ID immediately before commit.
- If a patch digest is retained, use binary-complete output and include modes, symlink
  targets, deletions, and filenames unambiguously.
- Add adversarial binary, symlink, rename, deletion, executable-bit, and special-file
  tests.

### 7. Commit scanning is incomplete and silently ignores reported surprises

Severity: **high**

Evidence:

- `scan_commit_set` scans only the first 1 MiB of a text file for credentials, so a
  credential later in an allowed-size file is missed.
- Oversized files, binary files, and symlinks are only placed in `warnings`.
- `_finalize_locked` checks only `report.safe` (refusals) and never surfaces or requires
  approval for `report.warnings`; those “surprises” are silently committed.

Required correction:

- Scan every byte of eligible text with bounded streaming logic and cross-chunk token
  handling.
- Fail closed on unexpected binary, oversized, symlink, device, FIFO, or socket paths
  unless a trusted repository-specific policy explicitly allows that exact case.
- Return the safety report in finalization evidence and test secrets beyond the first
  MiB plus each special-file class.

### 8. Crash recovery is claimed but not implemented

Severity: **high**

Evidence:

- A replacement worker always starts `_execute_locked` from `_prepare_worktree` and
  then transitions to `CLAUDE_IMPLEMENTING`; it has no phase-aware recovery dispatcher.
- Re-entering from any later durable phase produces an illegal backward transition.
- `_status_with_liveness` marks a vanished worker failed rather than offering verified
  recovery.
- The required crash-recovery tests for every transition are absent.

Required correction:

- Either implement explicit, idempotent, phase-aware crash recovery with durable phase
  inputs/outputs and integrity rechecks, or remove every recovery claim and document
  worker crash as terminal. Because `BUILD_SPEC.md` explicitly requires crash-recovery
  tests, the compliant choice is to implement it.

## Build, test, and installation failures

### 9. The current automated suite is red

Using `/home/jman/miniconda3/bin/python` with the source imported from `src`, I observed:

- unit suite: all unit tests passed;
- integration suite: **8 failed**;
- failing cases include child call-count/isolation assertions, working-directory
  assertions, exact critique capture, status persistence/schema validation, missing
  handoff gating, and cancellation gating.

Several failures come from the fake CLIs logging harmless `--version` probes as agent
turns. Fix the fixtures/assertions so probes are distinguishable from workflow calls;
do not delete meaningful assertions to make the suite green. The evidence-schema failure
is a real production defect, not only a test issue. The critique equality failure also
shows the coordinator strips the captured final message before archiving it; decide on
one exact canonical byte contract and test its digest consistently.

### 10. The required reproducible Python 3.12 build is missing

Evidence:

- `uv.lock` is absent.
- `pyproject.toml` now requires Python `>=3.13` and Ruff targets 3.13, while the build
  specification requires Python 3.12 and the README still says 3.12+.
- `requirements-lock.txt` is an ad-hoc secondary lock and does not satisfy the required
  committed `uv.lock` workflow.

Required correction:

- Restore the specified Python 3.12 target unless a current dependency gives concrete
  evidence that 3.12 is impossible; if so, stop and report that contradiction instead
  of silently changing the requirement.
- Generate and commit a valid `uv.lock`; prove `uv sync --frozen` works from a clean
  checkout on the target Python.
- Build a wheel and verify that it contains `agent_duet` and the three prompt resources,
  then install it into a clean environment and exercise the console entry point.

### 11. GitHub and host setup are not complete

Evidence from the current workspace and host:

- the repository has no commits (`main` is unborn);
- no Git remote is configured, including no `origin`;
- `~/.config/agent-duet/config.toml` is absent;
- neither Claude Code nor Codex lists an `agent_duet` MCP registration;
- therefore `doctor`, MCP Inspector, the five-tool host view, detached-worker reconnect,
  disposable real-CLI workflow, finalization, push verification, and the two-PC checks
  have not been completed.

Required correction:

- First finish and validate the code. Then create the initial commit, configure the
  expected private GitHub repository as `origin` without embedding credentials in the
  remote URL, push normally (never force), and verify local HEAD equals the remote ref.
- Use the already authenticated GitHub CLI/keyring state; do not place the credential
  supplied in chat in files, argv, logs, documentation, Git configuration, or commits.
- Install/configure/register only after the complete suite is green. Run the disposable
  end-to-end workflow from both interactive hosts before declaring the system ready.

## Additional correctness work required

- Add explicit tests for all five MCP tool schemas and annotations. `duet_status` is
  annotated read-only but may transition a dead-worker run to `FAILED`; either make the
  operation truly read-only or document and annotate the state mutation accurately.
- Enforce `duet_wait.timeout_seconds` as a strict 1..300 boundary instead of silently
  clamping invalid values.
- Validate configuration-file ownership/type and reject symlinks or group/world access,
  not merely warn in `doctor`; the file controls executable paths and trusted commands.
- Make atomic writes resistant to pre-created symlink attacks: use a unique temporary
  file opened with exclusive/no-follow semantics and verify the destination directory
  and artifact types. Reject handoff/critique symlinks.
- Ensure every exception after entering `FINALIZING` becomes a precise durable `FAILED`
  state with partial-state evidence; do not leave a run stuck because only selected
  exception classes are translated.
- Remove the unrelated demo files `impl.py`, `test_impl.py`,
  `CLAUDE_CRITIQUE_REQUEST.md`, and `GPT_CRITIQUE_FOR_CLAUDE.md` from the product tree.
  They are residue from a coordinator smoke scenario, not deliverables.

## Required completion sequence

1. Re-read `BUILD_SPEC.md` as acceptance criteria and undo every unauthorized security
   deviation.
2. Add failing regression tests for each confirmed issue above before changing the
   implementation.
3. Fix the smallest complete causal paths; do not paper over failures by weakening tests
   or documentation.
4. Run, from the specified reproducible environment:
   - frozen dependency sync;
   - formatter check;
   - Ruff;
   - strict mypy;
   - the full unit and integration suite;
   - wheel-content and clean-install smoke tests;
   - MCP Inspector/list-tools smoke test with exactly five tools;
   - disposable real Claude -> Codex -> Claude workflow, including attempted Codex write,
     client disconnect/reconnect, mid-child cancellation, `AWAITING_FINALIZE`, commit,
     push, and exact remote-SHA verification.
5. Inspect the real logs/artifacts for permissions, bounded size, redaction, and absence
   of credentials.
6. Only after all evidence is green, configure the private remote, stage only intended
   files, commit, push without force, and verify the remote SHA. Report exact commands,
   exit codes, commit SHA, host registration state, and anything not tested.

Do not claim “production-quality,” “secure,” “working,” “installed,” or “ready” until
all blockers above are resolved and the actual end-to-end workflow succeeds.
