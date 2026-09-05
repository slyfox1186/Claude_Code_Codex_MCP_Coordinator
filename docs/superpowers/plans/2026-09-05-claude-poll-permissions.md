# Claude Poll Permission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agent Duet polling deterministic in Claude Code auto mode without pre-approving any mutating or publishing tool.

**Architecture:** Add one focused Python settings helper with install, check, and remove operations, and call it from the existing Bash installer lifecycle. Preserve the MCP server's read-only annotations and 90-second wait behavior; solve the client-side rejection at Claude Code's earlier permission gate using two exact user-scoped rules.

**Tech Stack:** Bash, Python 3.13 standard library, Claude Code settings JSON, pytest.

---

## File map

- Create `src/claude_permissions.py`: validate, atomically update, check, and clean up the two exact Claude polling permissions.
- Create `tests/unit/test_claude_permissions.py`: focused settings semantics and failure tests.
- Modify `setup.sh`: use the helper for install, health check, and uninstall.
- Modify `tests/unit/test_setup_script.py`: black-box lifecycle regression coverage.
- Modify `commands/duet.md` and `tests/unit/test_prompts.py`: safe operator recovery from legacy/misconfigured installations.
- Modify `README.md`, `INSTALL.md`, `HOW_TO_BUILD_THIS.md`, and `HOW_TO_TEST.md`: installation, manual setup, health, and troubleshooting guidance.

### Task 1: Specify settings-helper behavior

**Files:**

- Create: `tests/unit/test_claude_permissions.py`
- Create: `src/claude_permissions.py`

- [ ] **Step 1: Write failing helper tests**

Add tests that import `agent_duet.claude_permissions` and require:

```python
REQUIRED_POLL_PERMISSIONS == (
    "mcp__agent_duet__duet_status",
    "mcp__agent_duet__duet_wait",
)
```

Test `install_permissions(path)` against a missing file and an existing object containing
unrelated keys, permission rules, Unicode, and mode `0o640`. Require exact-rule insertion,
preserved data/mode, a pre-change `.duet-backup`, and a second idempotent invocation that
does not rewrite either file. Require malformed JSON, a non-object root,
non-object `permissions`, and non-string `permissions.allow` entries to raise
`SettingsError` without modifying or backing up the input. Test `permissions_valid(path)`
before and after repair. Test `remove_permissions(path)` removes only the exact two rules
and preserves every unrelated value.

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/jman/miniconda3/envs/agent-duet-dev/bin/python -m pytest -q tests/unit/test_claude_permissions.py
```

Expected: collection fails because `agent_duet.claude_permissions` does not exist.

- [ ] **Step 3: Implement the minimal helper**

Create `src/claude_permissions.py` with `SettingsError`, the exact permission tuple,
`install_permissions`, `permissions_valid`, `remove_permissions`, and a `main()` accepting
`install|check|remove SETTINGS_PATH`. Parse with `json.loads`; validate each traversed
container explicitly. Serialize with `ensure_ascii=False`, two-space indentation, and one
trailing newline. Before a changed existing file, use `shutil.copy2` to create
`<settings>.duet-backup`. Write through `tempfile.mkstemp` in the same directory, flush and
`fsync`, apply the old mode or `0o600`, replace with `os.replace`, and `fsync` the directory.
Refuse symlinks and non-regular paths. Make `check` return one only for missing/invalid rules
and make parse/type failures print a concise error and return two.

- [ ] **Step 4: Verify GREEN**

Run the Task 1 test command. Expected: every helper test passes.

- [ ] **Step 5: Commit the helper cycle**

```bash
git add src/claude_permissions.py tests/unit/test_claude_permissions.py
git commit -m "fix: manage Claude polling permissions safely"
```

### Task 2: Integrate install, health, and uninstall

**Files:**

- Modify: `tests/unit/test_setup_script.py`
- Modify: `setup.sh`

- [ ] **Step 1: Write failing black-box lifecycle tests**

In the temporary-home setup tests, require a guided installation to create
`~/.claude/settings.json` with only the two exact Agent Duet allow rules, never
`mcp__agent_duet__*`. Seed an existing settings object and require setup to preserve it.
Run `setup.sh check` after removing one rule and require a nonzero result containing
`polling permissions are missing`. Run `setup.sh uninstall` after installation and require
both exact entries removed while an unrelated allow rule remains.

Update existing hand-built healthy fixtures with a helper that writes the two required
rules; do not weaken the new health assertion merely to preserve old fixtures.

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/jman/miniconda3/envs/agent-duet-dev/bin/python -m pytest -q tests/unit/test_setup_script.py -k 'poll_permission or health_check_surfaces or codex_health'
```

Expected: the new lifecycle tests fail because setup neither writes nor checks the rules.

- [ ] **Step 3: Wire the helper into setup**

Define:

```bash
CLAUDE_CONFIG_ROOT="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CLAUDE_COMMANDS_DIR="$CLAUDE_CONFIG_ROOT/commands"
CLAUDE_SETTINGS_FILE="$CLAUDE_CONFIG_ROOT/settings.json"
```

After successful Claude MCP registration, execute:

```bash
"$PY" "$REPO_ROOT/src/claude_permissions.py" install "$CLAUDE_SETTINGS_FILE"
```

and fail installation if the helper refuses the file. In `do_check`, run the helper's
`check` action and report a targeted repair command. In `do_uninstall`, obtain the existing
dedicated interpreter when available and run `remove`; if the environment is already gone,
finish unregistering but print the two exact stale rules the operator must remove manually.

- [ ] **Step 4: Verify GREEN**

Run the Task 2 command, then the entire setup-script module. Expected: all tests pass.

- [ ] **Step 5: Commit the installer cycle**

```bash
git add setup.sh tests/unit/test_setup_script.py
git commit -m "fix: preapprove read-only Claude polling tools"
```

### Task 3: Make classifier-denial recovery truthful

**Files:**

- Modify: `tests/unit/test_prompts.py`
- Modify: `commands/duet.md`

- [ ] **Step 1: Write the failing prompt contract test**

Require the command to name both canonical permissions, `/permissions`, and
`./setup.sh install`. Require explicit prohibitions against editing
`~/.claude/settings.json`, creating `.claude/settings.local.json`, inferring state from
repository files, and repeatedly retrying a denied call.

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/jman/miniconda3/envs/agent-duet-dev/bin/python -m pytest -q tests/unit/test_prompts.py -k auto_mode
```

Expected: failure because the command has only generic stale-session recovery.

- [ ] **Step 3: Add bounded operator recovery**

Extend the wait failure paragraph: when the returned error says the auto-mode classifier
denied `duet_wait` or `duet_status`, retain and report the run ID, mark current status
stale/unverified, stop tool retries, and tell the operator to add the two exact rules via
`/permissions` or run `./setup.sh install` outside the blocked session. State that Claude
must not edit protected settings, create project-local settings, or inspect changing files
as a substitute for status evidence. Resume only after the operator reports the permission
repair.

- [ ] **Step 4: Verify GREEN and rendered text**

Run the Task 3 test command and inspect `commands/duet.md` directly for correctly delimited
literal names and paths. Expected: tests pass and every machine-readable token is in
backticks.

- [ ] **Step 5: Commit the prompt cycle**

```bash
git add commands/duet.md tests/unit/test_prompts.py
git commit -m "fix: guide safe recovery from Claude auto-mode denial"
```

### Task 4: Document the permission boundary

**Files:**

- Modify: `README.md`
- Modify: `INSTALL.md`
- Modify: `HOW_TO_BUILD_THIS.md`
- Modify: `HOW_TO_TEST.md`
- Modify: `tests/unit/test_setup_script.py`

- [ ] **Step 1: Extend documentation assertions**

Require all installation guides to name the exact read-only permission purpose. Require
README's manual setup to contain both canonical rules and the troubleshooting tables to
map `Denied by auto mode classifier` to `./setup.sh install` plus `/permissions`.

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/jman/miniconda3/envs/agent-duet-dev/bin/python -m pytest -q tests/unit/test_setup_script.py -k documentation
```

Expected: failure because the current guides describe only MCP registration and timeouts.

- [ ] **Step 3: Update the four guides**

Explain that setup adds exact user-scoped allow rules only for `duet_status` and
`duet_wait`, while start/cancel/finalize remain governed normally. Add the rules to manual
installation, update health-check expectations, and add the recovery row without copying
the same explanation into unrelated sections.

- [ ] **Step 4: Verify GREEN and inspect Markdown**

Run the Task 4 command and read each changed section in its rendered order. Expected:
tests pass, tables remain aligned, and no guide suggests allowing the whole server.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md INSTALL.md HOW_TO_BUILD_THIS.md HOW_TO_TEST.md tests/unit/test_setup_script.py
git commit -m "docs: explain Claude polling permission boundary"
```

### Task 5: Critical completion audit, live repair, and delivery

**Files:** Review the complete branch and current installation.

- [ ] **Step 1: Run focused and full static verification**

```bash
/home/jman/miniconda3/envs/agent-duet-dev/bin/python -m pytest
/home/jman/miniconda3/envs/agent-duet-dev/bin/python -m ruff check .
/home/jman/miniconda3/envs/agent-duet-dev/bin/python -m mypy
bash -n setup.sh
git diff --check origin/main...HEAD
```

Expected: zero pytest failures, `All checks passed!`, no mypy issues, Bash exit zero, and
no whitespace errors.

- [ ] **Step 2: Exercise an isolated real-script lifecycle**

Use a temporary `HOME` with fake Claude/Codex/Agent Duet commands, run setup, check, rerun
setup for idempotency, and uninstall. Inspect the actual settings JSON after each step.
Expected: install adds exactly two rules, check succeeds, rerun does not duplicate them,
and uninstall removes only them.

- [ ] **Step 3: Repair and verify this machine**

Run `./setup.sh install` from the verified checkout, then `./setup.sh check`. Inspect only
`permissions.allow` from `~/.claude/settings.json`; require both exact entries and reject
`mcp__agent_duet__*`. Call `duet_status` for the retained SaidProof run and report only its
returned phase/liveness evidence.

- [ ] **Step 4: Audit and integrate**

Re-read the design and request, inspect every diff and affected lifecycle branch, then use
the finishing-a-development-branch workflow to integrate the feature branch into local
`main`. Fetch `origin`, rebase safely without force, and rerun any checks invalidated by
integration.

- [ ] **Step 5: Push and verify GitHub state**

```bash
git push origin main
git rev-parse HEAD
git ls-remote origin refs/heads/main
git status --short --branch
```

Expected: the local and remote 40-character SHAs match, push succeeds without force, and
the main worktree is clean and tracking `origin/main`.
