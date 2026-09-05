# Concurrent Project Runs Design

**Date:** 2026-09-05

## Problem

Agent Duet currently defaults `max_parallel_global` to `1`. `create_run()` applies that
global ceiling before its existing per-repository duplicate guard, so one active project
blocks a different project. The live installation has that exact value and therefore
reproduces the reported behavior.

## Design

Allow two active runs globally by default while retaining exactly one active run per
canonical repository. The configured global ceiling remains authoritative and continues
to accept values from 1 through 16, so operators can reduce or raise concurrency for their
machine and provider limits. A third concurrent repository is refused at the new default
with the active run IDs and recovery guidance.

Reserve both the global slot and repository slot in the same SQLite `BEGIN IMMEDIATE`
transaction that inserts the durable run. This prevents separate MCP server processes from
passing independent preflight counts at the same instant. Before reserving, reap provably
dead runs across all active repositories so a crashed project does not consume another
project's capacity indefinitely.

New configurations generated from `config.example.toml` receive the new default. Existing
explicit configurations are not silently migrated because `1` may be an intentional
resource or spending limit. This machine's configuration will be changed to `2` because
the operator explicitly requested concurrent projects.

## Verification

Regression tests must prove that two distinct repositories can reserve slots under the
default configuration, a third is refused, an explicit ceiling of one still refuses the
second, and the same repository never receives two active runs. Threaded tests must prove
global and per-repository limits remain true under simultaneous transactions. Run the
complete Python, Ruff, mypy, Bash, installer, and live doctor checks before pushing `main`.
