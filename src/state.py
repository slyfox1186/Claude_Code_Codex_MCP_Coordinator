#!/usr/bin/env python3
"""Durable run state in SQLite (WAL mode).

Every phase transition is written inside a transaction together with its timestamp and
reason, and illegal transitions are refused at this layer. Progress is never inferred
from a live stdio connection: if it is not in this database, it did not happen.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from .models import TERMINAL_PHASES, Evidence, Phase, RunStatus, transition_allowed

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id               TEXT PRIMARY KEY,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    phase                TEXT NOT NULL,
    terminal             INTEGER NOT NULL DEFAULT 0,
    repo_path            TEXT NOT NULL,
    git_common_dir       TEXT NOT NULL,
    worktree             TEXT,
    branch               TEXT,
    base_sha             TEXT,
    current_sha          TEXT,
    delivery_mode        TEXT NOT NULL,
    task                 TEXT NOT NULL,
    acceptance_criteria  TEXT NOT NULL DEFAULT '[]',
    idempotency_key      TEXT,
    summary              TEXT NOT NULL DEFAULT '',
    error                TEXT,
    evidence             TEXT NOT NULL DEFAULT '{}',
    validated_diff_sha256 TEXT,
    validated_tree_sha   TEXT,
    owned_paths          TEXT NOT NULL DEFAULT '[]',
    worker_pid           INTEGER,
    worker_pgid          INTEGER,
    worker_start_ticks   TEXT,
    active_child_pid     INTEGER,
    active_child_pgid    INTEGER,
    active_child_ticks   TEXT,
    active_child_label   TEXT,
    cancel_requested     INTEGER NOT NULL DEFAULT 0,
    run_dir              TEXT NOT NULL,
    host                 TEXT NOT NULL DEFAULT '',
    server_version       TEXT NOT NULL DEFAULT '',
    claude_version       TEXT NOT NULL DEFAULT '',
    codex_version        TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS runs_idempotency
    ON runs (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS runs_repo_phase ON runs (repo_path, phase);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    at      TEXT NOT NULL,
    phase   TEXT NOT NULL,
    reason  TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE INDEX IF NOT EXISTS events_run ON events (run_id, id);
"""


def utcnow() -> str:
    """Return an ISO-8601 UTC timestamp with second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateError(RuntimeError):
    """Raised for illegal transitions, unknown runs, and idempotency conflicts."""


@dataclass(slots=True)
class RunRecord:
    """One row of ``runs`` in Python form."""

    run_id: str
    created_at: str
    updated_at: str
    phase: Phase
    terminal: bool
    repo_path: str
    git_common_dir: str
    delivery_mode: str
    task: str
    run_dir: str
    acceptance_criteria: list[str] = field(default_factory=list)
    worktree: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    current_sha: str | None = None
    idempotency_key: str | None = None
    summary: str = ""
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    validated_diff_sha256: str | None = None
    validated_tree_sha: str | None = None
    owned_paths: list[str] = field(default_factory=list)
    worker_pid: int | None = None
    worker_pgid: int | None = None
    worker_start_ticks: str | None = None
    #: The child agent currently running, if any. A child lives in its OWN process
    #: group, so cancelling must signal this group explicitly; killing the worker's
    #: group alone would orphan a fully privileged agent process.
    active_child_pid: int | None = None
    active_child_pgid: int | None = None
    active_child_ticks: str | None = None
    active_child_label: str | None = None
    cancel_requested: bool = False
    host: str = ""
    server_version: str = ""
    claude_version: str = ""
    codex_version: str = ""

    def to_status(self, next_action: str = "") -> RunStatus:
        """Project this row into the MCP-facing status shape."""
        return RunStatus(
            run_id=self.run_id,
            phase=self.phase,
            terminal=self.terminal,
            repo=self.repo_path,
            worktree=self.worktree,
            branch=self.branch,
            base_sha=self.base_sha,
            current_sha=self.current_sha,
            delivery_mode=self.delivery_mode,
            created_at=self.created_at,
            updated_at=self.updated_at,
            summary=self.summary,
            error=self.error,
            evidence=Evidence.from_record(self.evidence or {}),
            next_action=next_action or default_next_action(self.phase),
        )


def default_next_action(phase: Phase) -> str:
    """Return the standard operator guidance for ``phase``."""
    match phase:
        case Phase.AWAITING_FINALIZE:
            return (
                "Summarize the returned evidence for the user and ask for approval "
                "before calling duet_finalize. Do not imply commit, push, or deploy "
                "has happened."
            )
        case Phase.COMPLETE:
            return "Run finished. Report the exact commit and remote SHAs."
        case Phase.FAILED:
            return "Run failed. Report the error and the preserved evidence; do not retry blindly."
        case Phase.CANCELLED:
            return "Run cancelled. Nothing was committed, pushed, or deployed."
        case _:
            return "Still running. Call duet_wait with this run_id for a bounded wait."


class StateStore:
    """Thin, explicit SQLite wrapper. One instance per process is enough."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._init_db()

    # -- connection plumbing ----------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection; commits on success, rolls back on error."""
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside an IMMEDIATE transaction."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            # Additive migration: columns introduced after the first release.
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            for column, decl in (
                ("validated_tree_sha", "TEXT"),
                ("active_child_pid", "INTEGER"),
                ("active_child_pgid", "INTEGER"),
                ("active_child_ticks", "TEXT"),
                ("active_child_label", "TEXT"),
            ):
                if column not in existing:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {decl}")
        try:
            self.db_path.chmod(0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.db_path) + suffix)
                if sidecar.exists():
                    sidecar.chmod(0o600)
        except OSError:  # pragma: no cover - permissions are best effort on odd mounts
            pass

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            phase=Phase(row["phase"]),
            terminal=bool(row["terminal"]),
            repo_path=row["repo_path"],
            git_common_dir=row["git_common_dir"],
            delivery_mode=row["delivery_mode"],
            task=row["task"],
            run_dir=row["run_dir"],
            acceptance_criteria=json.loads(row["acceptance_criteria"]),
            worktree=row["worktree"],
            branch=row["branch"],
            base_sha=row["base_sha"],
            current_sha=row["current_sha"],
            idempotency_key=row["idempotency_key"],
            summary=row["summary"],
            error=row["error"],
            evidence=json.loads(row["evidence"]),
            validated_diff_sha256=row["validated_diff_sha256"],
            validated_tree_sha=row["validated_tree_sha"],
            owned_paths=json.loads(row["owned_paths"]),
            worker_pid=row["worker_pid"],
            worker_pgid=row["worker_pgid"],
            worker_start_ticks=row["worker_start_ticks"],
            active_child_pid=row["active_child_pid"],
            active_child_pgid=row["active_child_pgid"],
            active_child_ticks=row["active_child_ticks"],
            active_child_label=row["active_child_label"],
            cancel_requested=bool(row["cancel_requested"]),
            host=row["host"],
            server_version=row["server_version"],
            claude_version=row["claude_version"],
            codex_version=row["codex_version"],
        )

    def get(self, run_id: str | UUID) -> RunRecord:
        """Return one run, raising :class:`StateError` if it does not exist."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        if row is None:
            raise StateError(f"unknown run_id {run_id}")
        return self._row_to_record(row)

    def find_by_idempotency_key(self, key: str) -> RunRecord | None:
        """Return the run previously created with ``key``, if any."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def active_runs(self, repo_path: str | None = None) -> list[RunRecord]:
        """Return non-terminal runs, optionally restricted to one repository."""
        terminal = tuple(p.value for p in TERMINAL_PHASES)
        placeholders = ",".join("?" * len(terminal))
        sql = f"SELECT * FROM runs WHERE phase NOT IN ({placeholders})"  # noqa: S608
        params: list[Any] = list(terminal)
        if repo_path:
            sql += " AND repo_path = ?"
            params.append(repo_path)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def all_runs(self) -> list[RunRecord]:
        """Return every run, newest first."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [self._row_to_record(row) for row in rows]

    def events(self, run_id: str | UUID) -> list[tuple[str, str, str]]:
        """Return ``(at, phase, reason)`` triples for one run, in order."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT at, phase, reason FROM events WHERE run_id = ? ORDER BY id",
                (str(run_id),),
            ).fetchall()
        return [(r["at"], r["phase"], r["reason"]) for r in rows]

    # -- writes ------------------------------------------------------------

    def create_run(self, record: RunRecord) -> RunRecord:
        """Insert a new run row and its first event, atomically."""
        now = utcnow()
        record.created_at = record.created_at or now
        record.updated_at = now
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, created_at, updated_at, phase, terminal, repo_path,
                        git_common_dir, worktree, branch, base_sha, current_sha,
                        delivery_mode, task, acceptance_criteria, idempotency_key,
                        summary, error, evidence, validated_diff_sha256,
                        validated_tree_sha, owned_paths,
                        worker_pid, worker_pgid, worker_start_ticks, cancel_requested,
                        run_dir, host, server_version, claude_version, codex_version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record.run_id,
                        record.created_at,
                        record.updated_at,
                        record.phase.value,
                        int(record.terminal),
                        record.repo_path,
                        record.git_common_dir,
                        record.worktree,
                        record.branch,
                        record.base_sha,
                        record.current_sha,
                        record.delivery_mode,
                        record.task,
                        json.dumps(record.acceptance_criteria),
                        record.idempotency_key,
                        record.summary,
                        record.error,
                        json.dumps(record.evidence),
                        record.validated_diff_sha256,
                        record.validated_tree_sha,
                        json.dumps(record.owned_paths),
                        record.worker_pid,
                        record.worker_pgid,
                        record.worker_start_ticks,
                        int(record.cancel_requested),
                        record.run_dir,
                        record.host,
                        record.server_version,
                        record.claude_version,
                        record.codex_version,
                    ),
                )
                conn.execute(
                    "INSERT INTO events (run_id, at, phase, reason) VALUES (?,?,?,?)",
                    (record.run_id, now, record.phase.value, "run created"),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError(f"could not create run: {exc}") from exc
        return record

    def transition(
        self,
        run_id: str | UUID,
        new_phase: Phase,
        *,
        reason: str = "",
        summary: str | None = None,
        error: str | None = None,
        evidence: dict[str, Any] | None = None,
        **columns: Any,
    ) -> RunRecord:
        """Move a run to ``new_phase``, refusing any edge outside the state machine."""
        run_key = str(run_id)
        now = utcnow()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_key,)
            ).fetchone()
            if row is None:
                raise StateError(f"unknown run_id {run_key}")
            current = Phase(row["phase"])
            if current == new_phase:
                pass  # Idempotent re-entry: allowed, still recorded as an event.
            elif not transition_allowed(current, new_phase):
                raise StateError(f"illegal transition {current.value} -> {new_phase.value}")

            merged_evidence = json.loads(row["evidence"])
            if evidence:
                merged_evidence.update(evidence)

            assignments = {
                "phase": new_phase.value,
                "terminal": int(new_phase in TERMINAL_PHASES),
                "updated_at": now,
                "evidence": json.dumps(merged_evidence),
            }
            if summary is not None:
                assignments["summary"] = summary
            if error is not None:
                assignments["error"] = error
            for name, value in columns.items():
                if name in {"acceptance_criteria", "owned_paths"}:
                    assignments[name] = json.dumps(value)
                elif isinstance(value, bool):
                    assignments[name] = int(value)
                else:
                    assignments[name] = value

            clause = ", ".join(f"{name} = ?" for name in assignments)
            conn.execute(
                f"UPDATE runs SET {clause} WHERE run_id = ?",  # noqa: S608
                (*assignments.values(), run_key),
            )
            conn.execute(
                "INSERT INTO events (run_id, at, phase, reason) VALUES (?,?,?,?)",
                (run_key, now, new_phase.value, reason),
            )
            updated = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_key,)
            ).fetchone()
        return self._row_to_record(updated)

    def update(self, run_id: str | UUID, **columns: Any) -> RunRecord:
        """Update non-phase columns without recording a transition."""
        if not columns:
            return self.get(run_id)
        run_key = str(run_id)
        assignments: dict[str, Any] = {"updated_at": utcnow()}
        for name, value in columns.items():
            if name in {"acceptance_criteria", "owned_paths"} or name == "evidence":
                assignments[name] = json.dumps(value)
            elif isinstance(value, bool):
                assignments[name] = int(value)
            else:
                assignments[name] = value
        clause = ", ".join(f"{name} = ?" for name in assignments)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE runs SET {clause} WHERE run_id = ?",  # noqa: S608
                (*assignments.values(), run_key),
            )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run_id {run_key}")
        return self.get(run_key)

    def merge_evidence(self, run_id: str | UUID, evidence: dict[str, Any]) -> RunRecord:
        """Shallow-merge ``evidence`` into the stored evidence blob."""
        run_key = str(run_id)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT evidence FROM runs WHERE run_id = ?", (run_key,)
            ).fetchone()
            if row is None:
                raise StateError(f"unknown run_id {run_key}")
            merged = json.loads(row["evidence"])
            merged.update(evidence)
            conn.execute(
                "UPDATE runs SET evidence = ?, updated_at = ? WHERE run_id = ?",
                (json.dumps(merged), utcnow(), run_key),
            )
        return self.get(run_key)

    def set_active_child(
        self,
        run_id: str | UUID,
        *,
        pid: int | None,
        pgid: int | None,
        ticks: str | None,
        label: str | None,
    ) -> None:
        """Record (or clear, with all-None) the child agent process currently running."""
        self.update(
            run_id,
            active_child_pid=pid,
            active_child_pgid=pgid,
            active_child_ticks=ticks,
            active_child_label=label,
        )

    def clear_active_child(self, run_id: str | UUID) -> None:
        """Forget the active child once it has exited."""
        self.set_active_child(run_id, pid=None, pgid=None, ticks=None, label=None)

    def request_cancel(self, run_id: str | UUID) -> RunRecord:
        """Set the cooperative cancel flag; the worker polls it between steps."""
        return self.update(run_id, cancel_requested=True)

    def delete_runs(self, run_ids: Sequence[str]) -> int:
        """Forget these runs entirely: their rows and their event history.

        Only ``gc`` calls this, and only for terminal runs past its cutoff. Without it
        the listing grows forever -- a row whose artifacts are long gone still shows up
        in ``agent-duet runs``, pointing at a repository that may no longer exist. It
        refuses to touch a non-terminal run so a live run can never be erased out from
        under its worker.
        """
        if not run_ids:
            return 0
        deleted = 0
        with self.transaction() as conn:
            for run_id in run_ids:
                row = conn.execute(
                    "SELECT terminal FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None or not row["terminal"]:
                    continue
                conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
                deleted += 1
        return deleted

    def wait_for_change(
        self, run_id: str | UUID, *, since: str, timeout_seconds: int
    ) -> RunRecord:
        """Poll until ``updated_at`` differs from ``since``, a terminal/awaiting phase
        is reached, or the bounded timeout expires. Always returns a record."""
        deadline = time.monotonic() + timeout_seconds
        record = self.get(run_id)
        while True:
            if record.terminal or record.phase is Phase.AWAITING_FINALIZE:
                return record
            if record.updated_at != since:
                return record
            if time.monotonic() >= deadline:
                return record
            time.sleep(0.5)
            record = self.get(run_id)


def new_run_dir(runs_root: Path, run_id: str) -> Path:
    """Create and return the private per-run artifact directory."""
    path = runs_root / run_id
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    os.umask(os.umask(0o077))  # Touch umask so child writes default to private too.
    return path
