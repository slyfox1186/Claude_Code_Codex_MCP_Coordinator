# Project Validation Environments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every automatically configured Python validation command runnable in an isolated project environment and give one failed final validation a bounded evidence-driven repair pass.

**Architecture:** `setup.sh` owns project-environment discovery and dependency installation, while `Worker` owns prompt disclosure, durable failure evidence, and the single repair cycle. Existing configuration and state models remain the source of truth.

**Tech Stack:** Bash, Python 3.13, Conda/venv, pip, FastMCP, Pydantic, SQLite, pytest.

---

### Task 1: Project dependency installation

**Files:**
- Modify: `setup.sh`
- Modify: `tests/unit/test_setup_script.py`
- Modify: `README.md`
- Modify: `INSTALL.md`
- Modify: `HOW_TO_BUILD_THIS.md`
- Modify: `HOW_TO_TEST.md`

- [ ] Add black-box tests proving Python registration never writes Agent Duet's interpreter, reuses a pytest-capable project environment, and otherwise installs all discovered declared dependencies into a project-isolated environment after consent.
- [ ] Run the focused tests and verify they fail for the current `sys.executable` behavior.
- [ ] Add deterministic project-environment naming, dependency-manifest discovery, consent text, installation, import verification, and generated configuration.
- [ ] Run the focused tests and shell syntax check until green.
- [ ] Document the files setup reads, the isolation guarantee, consent behavior, and repair command.

### Task 2: Exact validation commands in model prompts

**Files:**
- Modify: `src/worker.py`
- Modify: `src/prompts/claude_implement.md`
- Modify: `src/prompts/claude_reconcile.md`
- Modify: `tests/unit/test_prompts.py`

- [ ] Add failing tests requiring both Claude prompts to contain the repository's exact configured command vectors.
- [ ] Verify the tests fail because the prompt contract currently says only “relevant validation.”
- [ ] Format and inject the authoritative vectors without shell interpolation or execution.
- [ ] Verify the focused prompt tests pass.

### Task 3: Bounded validation repair

**Files:**
- Modify: `src/models.py`
- Modify: `src/worker.py`
- Modify: `src/prompts/claude_validation_repair.md`
- Modify: `tests/integration/test_workflow.py`
- Modify: `tests/integration/test_regressions.py`

- [ ] Add failing integration tests proving the first failed validation is persisted in evidence and triggers one repair, a repaired tree is fully revalidated, and a second failure terminates.
- [ ] Verify each test fails for the current immediate `PhaseFailure` behavior.
- [ ] Add the repair phase/prompt and a two-attempt validation loop with immutable attempt artifacts.
- [ ] Verify focused worker and workflow tests pass.

### Task 4: Full verification and shipping

**Files:**
- Modify: `requirements-lock.txt` only if the coordinator setup itself now imports a new runtime package.

- [ ] Run `bash -n setup.sh` and `git diff --check`.
- [ ] Run the full test suite with `/home/jman/miniconda3/envs/agent-duet-dev/bin/python -m pytest`.
- [ ] Run Ruff and strict mypy with the same environment.
- [ ] Run `./setup.sh add-repo /home/jman/tmp/gemmabot-medical-compliant` interactively or with explicit consent and inspect the generated command.
- [ ] Run that exact generated validation command far enough to prove collection imports succeed.
- [ ] Reinstall Agent Duet, run `./setup.sh check`, commit only intended paths on `main`, fetch/rebase, push without force, and verify local `HEAD` equals `origin/main`.
