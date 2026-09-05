# Claude Poll Permission Design

**Date:** 2026-09-05

## Problem and evidence

Claude Code auto mode intermittently denied `duet_wait` and then denied the required
`duet_status` recovery call. The Agent Duet server log recorded the preceding successful
wait but no invocation for either denied call, proving the rejection occurred in Claude
Code before the MCP server received a request. Both tools already advertise read-only,
non-destructive, idempotent MCP annotations and perform no durable writes.

The installer registers the `agent_duet` MCP server but does not grant its polling tools
permission in Claude Code. The user's `~/.claude/settings.json` likewise has no matching
entries. Claude Code's documented permission order evaluates exact allow rules before the
auto-mode classifier; it also protects user settings from model-initiated edits. That
explains both the polling failure and Claude's failed attempt to repair its own authority.

## Design

During installation, merge these exact rules into the user-scoped
`permissions.allow` array in `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json`:

- `mcp__agent_duet__duet_status`
- `mcp__agent_duet__duet_wait`

Do not grant `mcp__agent_duet__*`. Starting, cancelling, and finalizing runs have side
effects and must retain Claude Code's ordinary permission and auto-mode checks. Exact
polling rules solve the proven failure without weakening the publication boundary.

The settings update must preserve every unrelated key and permission rule, reject malformed
or incorrectly typed JSON instead of replacing it, create a backup before changing an
existing file, use an atomic same-directory replacement, preserve the existing file mode,
and be idempotent. Coordinate Agent Duet writers with a directory lock, compare the live
file to the parsed byte snapshot before replacement, and retry from fresh state if another
writer wins. A newly created settings file is private to the user. Uninstall removes only
the two Agent Duet rules and leaves the rest of the settings object unchanged.

`setup.sh check` validates the two exact user rules in addition to the MCP connection. It
fails on matching user-level `ask` or `deny` rules and directs the operator to
`/permissions`; it does not remove them. Project and managed policy remains authoritative
and is explicitly outside what a user-settings file check can prove.

## Recovery and documentation

The `/duet` command must not respond to this denial by editing protected Claude settings,
creating project-local settings, guessing run state from files, or repeatedly invoking the
classifier. It reports the retained run ID and tells the operator to add the two exact rules
through `/permissions` or rerun `./setup.sh install` outside the blocked session, then resume
polling.

README, installation, testing, and second-machine instructions document the permission
rules, why only read-only tools are pre-approved, the health check, and the need to restart
only when registration or command files changed. On Claude Code versions that watch an
existing user settings file, an operator-added permission can apply to the next tool call;
the docs do not rely on that behavior for installation correctness.

## Verification

Black-box installer tests use temporary homes and fake provider CLIs. They prove fresh-file
creation, preservation of existing settings, idempotent repair, malformed/type-invalid JSON
refusal without damage, concurrent-writer retry, higher-precedence conflict reporting,
missing-rule health failure, and uninstall cleanup. Prompt contract tests prohibit
self-edit recovery and require the exact operator repair path. After focused red/green
cycles, run full pytest, Ruff, mypy, Bash syntax, whitespace checks, an isolated installer
smoke test, the real setup health check, and a live read-only status call.
