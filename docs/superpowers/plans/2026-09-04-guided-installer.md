# Guided Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `./setup.sh` a consent-based guided bootstrapper and prompt for a real repository after the user declines the demo.

**Architecture:** Keep one public installer. Add focused Bash functions for consent, isolated Python environment selection, vendor CLI installation, authentication, and repository input; test them through isolated subprocesses with fake commands and temporary homes. Preserve every existing subcommand.

**Tech Stack:** Bash, Miniconda, Python 3.13, pytest, Git, Claude Code CLI, Codex CLI.

---

## File map

- Modify `setup.sh`: guided bootstrap, login assistance, locked install, repository prompt.
- Create `tests/unit/test_setup_script.py`: isolated installer tests with no network or real-home writes.
- Create `INSTALL.md`: three-command public guide.
- Modify `README.md`: concise default install path and consent disclosure.
- Modify `HOW_TO_TEST.md`: document the post-demo repository prompt.

### Task 1: Fix the missing repository prompt

**Files:**

- Create: `tests/unit/test_setup_script.py`
- Modify: `setup.sh:44-51,261-270,599-609`

- [ ] **Step 1: Write the failing tests**

Create a helper that uses a temporary `HOME`, fake `claude`, `codex`, `agent-duet`, and Python wrappers, and a pseudo-terminal to drive the interactive script. Create a real temporary Git repository. Answer `n` to the demo and provide its path.

Test absolute, relative, and literal `~/project` inputs. Assert the canonical repository path appears in the generated config. Test blank input separately and assert it skips registration without registering the Agent Duet source checkout.

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/jman/miniconda3/bin/python -m pytest -q tests/unit/test_setup_script.py -k repository
```

Expected: FAIL because the current default dispatch exits after the `n` answer.

- [ ] **Step 3: Implement the minimal fix**

Add a `prompt_for_repo` function that reads a path, safely expands only `~` and `~/`, and passes every other absolute or relative value unchanged to `do_add_repo`. Do not use `eval`. Blank input prints `./setup.sh add-repo /path/to/your/project` and returns.

Add an `else` branch after the demo prompt:

```bash
if ask_yes "Try it now on a throwaway project?"; then
  do_demo
else
  prompt_for_repo
fi
```

- [ ] **Step 4: Verify GREEN**

Run the focused command from Step 2. Expected: all repository prompt cases PASS.

### Task 2: Add isolated Conda-or-system Python setup

**Files:**

- Modify: `tests/unit/test_setup_script.py`
- Modify: `setup.sh:24-110,599-609`

- [ ] **Step 1: Write failing tests**

Test that detected Conda offers to create `agent-duet`, records `conda create --name agent-duet`, and never invokes an operation against `base`. Test reuse of a compatible existing named environment and repair of only that environment. Without Conda, test that compatible default `python3` offers a private virtual environment and that pip is never run against the system interpreter. Refusal must print `installation not completed` and exit 2. An old system Python must produce one concise error without offering or downloading Conda. The `install` repair subcommand must not create an environment.

- [ ] **Step 2: Verify RED**

```bash
/home/jman/miniconda3/bin/python -m pytest -q tests/unit/test_setup_script.py -k 'python_environment or conda or consent'
```

Expected: FAIL because setup currently installs directly into whichever interpreter it discovers.

- [ ] **Step 3: Implement isolated environment selection**

Add `ask_consent` with a conservative `[y/N]` default. Detect Conda from `CONDA_EXE`, an executable `command -v conda` result, or `$HOME/miniconda3/bin/conda`. Create or reuse the named environment with `conda create --name agent-duet -y python=3.13 pip`, and resolve its actual interpreter through `conda run` so custom `envs_dirs` configurations work. If repair is needed, target only `-n agent-duet`. Never use an inherited `DUET_PYTHON` or existing launcher as the guided installer target.

If Conda is absent, require the default `python3` to be 3.13 or newer and create a private virtual environment at `${XDG_DATA_HOME:-$HOME/.local/share}/agent-duet/venv`. All dependency installation then uses that environment's absolute Python. Never install Conda, call `sudo`, modify `base`, or run pip against system Python.

Call the bootstrap only from the default guided path. Keep `setup.sh install` validation-only.

- [ ] **Step 4: Verify GREEN**

Run the focused command from Step 2. Expected: all selected cases PASS.

### Task 3: Install and authenticate Claude Code and Codex

**Files:**

- Modify: `tests/unit/test_setup_script.py`
- Modify: `setup.sh` prerequisite section

- [ ] **Step 1: Write failing tests**

With fake tools, prove missing Claude requires consent and uses `https://claude.ai/install.sh`; missing Codex requires consent and uses `https://chatgpt.com/codex/install.sh`; existing tools are not reinstalled; failed auth checks offer login; SSH uses `codex login --device-auth`; and non-interactive setup prints pending login commands without launching them.

- [ ] **Step 2: Verify RED**

```bash
/home/jman/miniconda3/bin/python -m pytest -q tests/unit/test_setup_script.py -k 'claude or codex or login'
```

Expected: FAIL because setup currently requires both commands and does not check authentication.

- [ ] **Step 3: Implement CLI setup**

Print each official HTTPS source, expected user-local files, updater behavior, and any possible shell-profile change before obtaining consent. Download into the private temporary directory and run with Bash. Refresh command lookup from `PATH` and known user-local binary directories.

Check `claude auth status` and `codex login status`. Ask before starting authentication. Use `claude auth login`; use `codex login --device-auth` over SSH/headless and `codex login` otherwise. A non-interactive run prints the commands instead.

- [ ] **Step 4: Verify GREEN**

Run the focused command from Step 2. Expected: all selected cases PASS.

### Task 4: Install locked dependencies and preserve commands

**Files:**

- Modify: `tests/unit/test_setup_script.py`
- Modify: `setup.sh:82-246,441-481,599-609`

- [ ] **Step 1: Write failing tests**

Log fake Python invocations and assert this order:

```text
-m pip install --quiet -r <repo>/requirements-lock.txt
-m pip install --quiet --no-deps --editable <repo>
```

Assert `install`, `add-repo`, `remove-repo`, `check`, `demo --clean`, and `uninstall` retain their dispatch semantics and do not unexpectedly start bootstrap.

- [ ] **Step 2: Verify RED**

```bash
/home/jman/miniconda3/bin/python -m pytest -q tests/unit/test_setup_script.py -k 'locked or dispatch or repair'
```

Expected: FAIL because setup currently skips the runtime lock.

- [ ] **Step 3: Implement minimal behavior**

Require `requirements-lock.txt`, install it with the selected absolute interpreter, then install Agent Duet editable with `--no-deps`. Keep registered MCP command paths absolute. Extract dispatch into `main()` and call it only when the script is executed so tests may source helpers safely.

- [ ] **Step 4: Verify GREEN**

```bash
/home/jman/miniconda3/bin/python -m pytest -q tests/unit/test_setup_script.py
```

Expected: all installer tests PASS.

### Task 5: Add concise installation documentation

**Files:**

- Create: `INSTALL.md`
- Modify: `README.md:55-76,256-273`
- Modify: `HOW_TO_TEST.md:1-30`
- Modify: `tests/unit/test_setup_script.py`

- [ ] **Step 1: Write failing documentation tests**

Assert `INSTALL.md` and README show the clone, cd, and `./setup.sh` commands; disclose consent before creating the isolated Python environment or installing Claude Code or Codex; state that detected Conda uses a dedicated `agent-duet` environment and is never installed; link to `SECURITY.md`; and place normal installation before advanced/manual material.

- [ ] **Step 2: Verify RED**

```bash
/home/jman/miniconda3/bin/python -m pytest -q tests/unit/test_setup_script.py -k documentation
```

Expected: FAIL because `INSTALL.md` does not exist.

- [ ] **Step 3: Write the docs**

Create `INSTALL.md` with the three commands first, a short description of prompts, `./setup.sh check`, and a compact troubleshooting table. Rewrite README's install section to match and link to `INSTALL.md`. Update `HOW_TO_TEST.md` to explain the real-repository prompt after demo refusal.

- [ ] **Step 4: Verify GREEN**

Run the complete installer test module. Expected: all cases PASS.

### Task 6: Full verification and audit

**Files:** Review all modified files.

- [ ] **Step 1: Run focused verification**

```bash
bash -n setup.sh
/home/jman/miniconda3/bin/python -m pytest -q tests/unit/test_setup_script.py
```

Exercise refusal, installer failure, demo refusal with relative repository input, and blank repository input. Expected: documented results with no real network or writes outside temporary homes.

- [ ] **Step 2: Run the full quality suite**

```bash
/home/jman/miniconda3/bin/python -m pytest
/home/jman/miniconda3/bin/python -m ruff check .
/home/jman/miniconda3/bin/python -m mypy
```

Expected: zero failures, `All checks passed!`, and no mypy issues.

- [ ] **Step 3: Audit the complete change**

Re-read the request and design. Inspect success, refusal, failure, SSH, and non-interactive paths. Confirm no `sudo`, system Python, credential output, unsafe `eval`, or unrelated refactor. Run:

```bash
git diff --check
git status --short
git diff --stat
git diff
```

Expected: only intended installer, tests, and documentation changes with clean whitespace.
