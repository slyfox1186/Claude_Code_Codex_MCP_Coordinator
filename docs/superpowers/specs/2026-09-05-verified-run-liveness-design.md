# Verified Run Liveness Design

**Date:** 2026-09-05

## Problem

An operator saw `/duet` repeat a model phase for a run ID that had no durable run row,
worker, child model process, or artifacts. The status contract exposed a phase but no
positive process-liveness evidence, while the command prompt did not prescribe a bounded
recovery when a wait result was lost or stale. Setup also replaced MCP registrations and
command files without explicitly telling already-open clients to restart.

The completion audit found a related proven cleanup defect: when a worker vanished,
automatic reaping marked the run failed without terminating its separately grouped child
model process. A failed termination also cleared that child's stored identity, preventing a
safe retry and making later status look cleaner than reality.
The server-crash window between creating a `QUEUED` row and recording its worker PID could
also leave a row that blocked every future run forever.

## Design

Add a server-derived `liveness` object to every returned `RunStatus`. It records when the
check occurred, whether the detached worker and current child model were verified alive,
and one explicit state: starting, model active, coordinator active, transitioning, awaiting
the operator, finalizing, finished, or worker missing. A vanished worker continues to be
projected as terminal failure without mutating the read-only status path. A live worker
without the expected child is reported as transitioning, never as an actively working model.

Tighten `commands/duet.md` and the MCP server instructions: progress may be narrated only
from a successfully returned status whose `run_id` matches the retained ID. If a wait errors,
loses its background result, or returns no matching status, make one `duet_status` recovery
call. If that also fails, report the session as stale/unverified and stop; never infer phases,
files, or changes from a spinner or prior narrative.

After installation, tell users to restart any Claude Code or Codex session that was already
open because those processes retain the MCP subprocess and `/duet` prompt loaded at startup.

## Compatibility and Safety

The liveness object is additive. Phase timeouts and the 90-second foreground poll remain
unchanged, so better observability does not shorten model work. Process identity checks reuse
the existing PID plus Linux start-tick guard. No status read writes to SQLite.
The mutating dead-run repair path terminates the child before releasing the run slot and
keeps its identity when cleanup cannot be confirmed.
An unowned `QUEUED` row becomes `WORKER_MISSING` after a short startup grace period;
`FINALIZING` is exempt because it runs in the MCP server rather than a detached worker.
Terminal records with a live worker or child report `CLEANUP_REQUIRED`, and repeated cancellation
retries cleanup instead of returning a false no-op.
The same state covers a terminal record with a live worker or an approval-pending record
with a live child. Finalization independently refuses a live recorded child, so stale model
work cannot race a commit even if a caller disregards the status guidance.

## Verification

Regression tests cover a live expected child, missing/dead child, vanished worker, prompt
recovery rules, matching run IDs, and the setup restart notice. Run the full pytest, Ruff,
mypy, Bash syntax, whitespace, installer, and live health checks before pushing `main`.
