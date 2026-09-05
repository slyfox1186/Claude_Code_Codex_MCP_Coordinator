# Concurrent Project Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permit two different project repositories to run Agent Duet concurrently by default without allowing duplicate runs in one repository.

**Architecture:** Keep the existing global and per-repository guards. Raise only the default global capacity to two, retain explicit operator overrides, and document the two-level limit.

**Tech Stack:** Python 3.13, Pydantic, SQLite, Bash, pytest.

---

### Task 1: Prove the concurrency contract

**Files:**

- Modify: `tests/integration/test_regressions.py`
- Modify: `tests/unit/test_config.py`

- [x] Add a test that creates active runs in two distinct repositories under the default configuration and confirms both are accepted.
- [x] Extend the test to prove a third distinct repository is refused by `max_parallel_global`.
- [x] Keep an explicit-one test proving operators can still serialize all runs.
- [x] Prove a second run for the same repository remains refused.
- [x] Add threaded tests proving the limits are reserved atomically across MCP processes.
- [x] Run the focused tests and confirm the default-two and atomic-reservation tests fail before implementation.

### Task 2: Change the default and explain it

**Files:**

- Modify: `src/config.py`
- Modify: `config.example.toml`
- Modify: `README.md`
- Modify: `HOW_TO_BUILD_THIS.md`
- Modify: `HOW_TO_TEST.md`

- [x] Change the Pydantic and generated-example defaults from one to two.
- [x] Reserve capacity in the same SQLite transaction that inserts the run.
- [x] Reap provably dead runs across repositories before reserving global capacity.
- [x] Document two global slots, one slot per repository, the 1-16 override range, and upgrade behavior.
- [x] Run the focused tests and confirm they pass.

### Task 3: Verify, install, and ship

**Files:** Review every changed file.

- [x] Run the complete pytest suite, Ruff, mypy, `bash -n setup.sh`, and `git diff --check`.
- [x] Change this machine's explicit setting to `2`, reinstall, and verify doctor reports the configured concurrency.
- [x] Preserve `REDDIT_POST.md` and `setup_dir.sh` as untouched untracked files.
- [ ] Commit only intended files on `main`, fetch and rebase safely, push without force, and verify local `HEAD` equals GitHub `main`.
