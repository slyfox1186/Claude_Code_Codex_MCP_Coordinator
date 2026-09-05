# Verified Run Liveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every `/duet` progress claim traceable to a matching durable run and verified live processes.

**Architecture:** Add an additive MCP liveness projection computed from existing durable process identities. Make prompts recover once from a lost wait and otherwise fail closed, and tell users to restart open clients after setup replaces their MCP registration.

**Tech Stack:** Python 3.13, Pydantic, MCP 2.1, Bash, pytest.

---

### Task 1: Specify liveness at the MCP boundary

**Files:**

- Modify: `tests/integration/test_regressions.py`
- Modify: `src/models.py`
- Modify: `src/server.py`

- [x] Write tests proving an expected live child returns `MODEL_ACTIVE`, a missing or dead child returns `TRANSITIONING`, and a vanished worker returns `WORKER_MISSING` with terminal failure.
- [x] Run the focused tests and confirm they fail because `RunStatus` has no `liveness` field.
- [x] Add the bounded `RunLiveness` model and compute it with existing PID/start-tick checks in `_status_with_liveness`.
- [x] Return the liveness projection from start, idempotent start, wait, status, and cancel paths.
- [x] Terminate an orphan child before reaping a dead run, retain its identity after failed cleanup, and keep the run slot blocked until cleanup is confirmed.
- [x] Reap an abandoned `QUEUED` row after a bounded worker-start grace period without misclassifying server-owned `FINALIZING` work.
- [x] Report terminal orphan processes as `CLEANUP_REQUIRED` and make repeated cancellation retry cleanup.
- [x] Re-run the focused tests and confirm they pass.

### Task 2: Fail closed in the operator prompt

**Files:**

- Modify: `tests/unit/test_prompts.py`
- Modify: `commands/duet.md`
- Modify: `src/server.py`

- [x] Write prompt contract tests requiring an exact matching `run_id`, returned liveness, one recovery `duet_status`, and a stop after unverified recovery.
- [x] Run the prompt tests and confirm the new contract is absent.
- [x] Consolidate the command and compact MCP instructions around the new evidence rule.
- [x] Re-run prompt tests and confirm the server instructions remain at most 512 characters.

### Task 3: Prevent stale open-client upgrades

**Files:**

- Modify: `tests/unit/test_setup_script.py`
- Modify: `setup.sh`
- Modify: `README.md`
- Modify: `HOW_TO_BUILD_THIS.md`
- Modify: `HOW_TO_TEST.md`

- [x] Write an installer test requiring a clear restart notice.
- [x] Run it and confirm setup currently omits the notice.
- [x] Print the notice after successful setup and document why restart is necessary.
- [x] Re-run the focused installer test.

### Task 4: Verify and ship

**Files:** Review every changed file.

- [ ] Run the full pytest suite, Ruff, mypy, `bash -n setup.sh`, and `git diff --check`.
- [ ] Inspect the full diff and ensure `REDDIT_POST.md` remains untouched and unstaged.
- [ ] Install the updated package, run setup health checks, and inspect a real returned status schema.
- [ ] Commit only intended files on `main`, fetch and rebase safely, push without force, and verify local `HEAD` equals `origin/main`.
