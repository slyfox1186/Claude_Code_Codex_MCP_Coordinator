#!/usr/bin/env python3
"""``agent-duet logs`` and ``agent-duet runs``: one command that shows everything.

The point is that nobody should have to relay what happened by hand. One invocation
prints the process logs, the durable state timeline, the exact child command vectors,
the child stdout/stderr tails, the validation manifest, and the artifacts, all already
redacted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .logging_setup import default_state_dir, log_dir
from .models import Phase
from .process_guard import process_alive
from .redact import redact
from .state import RunRecord, StateStore

SEPARATOR = "=" * 78


def _section(title: str) -> None:
    print(f"\n{SEPARATOR}\n== {title}\n{SEPARATOR}")


def _print_tail(path: Path, lines: int) -> None:
    """Print the last ``lines`` lines of a file, redacted."""
    if not path.is_file():
        print(f"  (absent: {path})")
        return
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"  (unreadable: {exc})")
        return
    body = content.splitlines()
    shown = body[-lines:] if lines > 0 else body
    if len(body) > len(shown):
        print(f"  ... {len(body) - len(shown)} earlier line(s) omitted ...")
    for line in shown:
        print(f"  {redact(line)}")
    if not body:
        print("  (empty)")


def _load(config_path: Path | None) -> tuple[Config | None, Path]:
    """Return the config if it loads, plus the state directory to read logs from."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"WARNING: config could not be loaded ({exc}); falling back to the default "
              f"state directory")
        return None, default_state_dir()
    return config, config.state_path


def list_runs(config_path: Path | None = None) -> int:
    """Print one line per run, newest first."""
    config, _ = _load(config_path)
    if config is None:
        return 1
    store = StateStore(config.db_path)
    runs = store.all_runs()
    if not runs:
        print("no runs recorded")
        return 0
    print(f"{'RUN ID':38} {'PHASE':22} {'WORKER':9} {'UPDATED':21} REPO / BRANCH")
    for record in runs:
        print(
            f"{record.run_id:38} {record.phase.value:22} {_worker_state(record):9} "
            f"{record.updated_at:21} "
            f"{Path(record.repo_path).name} / {record.branch or '-'}"
        )
    return 0


def _worker_state(record: RunRecord) -> str:
    """Say whether a run is actually still being worked on.

    "Is it still running?" is the first question after a client session dies, and the
    phase alone cannot answer it: a crashed run sits at its last phase forever. The
    worker is detached and outlives the session that started it, so a live answer here
    is the difference between waiting and assuming the worst.
    """
    if record.terminal:
        return "-"
    if record.phase is Phase.AWAITING_FINALIZE:
        return "you"  # No worker by design; it is waiting on a person.
    if not record.worker_pid:
        return "?"
    if process_alive(record.worker_pid, record.worker_start_ticks):
        return "alive"
    return "DEAD"


def show_logs(
    run_id: str | None = None,
    *,
    tail: int = 200,
    full: bool = False,
    config_path: Path | None = None,
) -> int:
    """Print a complete diagnostic dump for the whole install, or for one run."""
    lines = 0 if full else tail
    config, state_dir = _load(config_path)

    _section(f"agent-duet {__version__} diagnostics")
    print(f"state dir : {state_dir}")
    if config is not None:
        print(f"config    : {config.source_path}")
        print(f"claude    : {config.claude_path}")
        print(f"codex     : {config.codex_path}")
        posture = (
            "dangerously-skip-permissions"
            if config.claude.dangerously_skip_permissions
            else config.claude.permission_mode
        )
        print(
            f"posture   : claude={posture} codex_sandbox={config.codex.sandbox_mode} "
            f"env={config.child_env_mode} log_level={config.log_level}"
        )

    logs = log_dir(state_dir)
    for name in ("server.log", "worker.log", "doctor.log"):
        _section(f"process log: {logs / name}")
        _print_tail(logs / name, lines)

    if config is None:
        return 1

    store = StateStore(config.db_path)
    records = store.all_runs()
    if run_id:
        records = [item for item in records if item.run_id.startswith(run_id)]
        if not records:
            print(f"\nno run matches {run_id!r}")
            return 1
    elif records:
        records = records[:1]  # Default to the newest run only.

    for record in records:
        _section(f"run {record.run_id}  [{record.phase.value}]")
        print(f"repo        : {record.repo_path}")
        print(f"worktree    : {record.worktree}")
        print(f"branch      : {record.branch}")
        print(f"base sha    : {record.base_sha}")
        print(f"current sha : {record.current_sha}")
        print(f"delivery    : {record.delivery_mode}")
        print(f"created     : {record.created_at}")
        print(f"updated     : {record.updated_at}")
        print(f"worker      : pid={record.worker_pid} pgid={record.worker_pgid}")
        print(f"cancel flag : {record.cancel_requested}")
        print(f"summary     : {redact(record.summary)}")
        if record.error:
            print(f"error       : {redact(record.error)}")
        print(f"owned paths : {record.owned_paths}")
        print(f"validated   : {record.validated_diff_sha256}")
        print("\ntask:")
        for line in redact(record.task).splitlines():
            print(f"  {line}")
        print("\nevidence:")
        print("  " + redact(json.dumps(record.evidence, indent=2)).replace("\n", "\n  "))

        print("\ntimeline:")
        for at, phase, reason in store.events(record.run_id):
            print(f"  {at}  {phase:22} {redact(reason)}")

        run_dir = Path(record.run_dir)
        if not run_dir.is_dir():
            print(f"\n(run directory {run_dir} is gone)")
            continue

        for argv_file in sorted(run_dir.glob("*.argv.json")):
            print(f"\ncommand vector {argv_file.name}:")
            try:
                argv = json.loads(argv_file.read_text())
                for part in argv:
                    print(f"  {part}")
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  (unreadable: {exc})")

        for stream in sorted(run_dir.glob("*.log")):
            _section(f"{record.run_id[:8]} :: {stream.name}")
            _print_tail(stream, lines)

        manifests = sorted(run_dir.glob("validation-attempt-*-manifest.json"))
        legacy_manifest = run_dir / "validation-manifest.json"
        if legacy_manifest.is_file():
            manifests.insert(0, legacy_manifest)
        for manifest in manifests:
            _section(f"{record.run_id[:8]} :: {manifest.name}")
            _print_tail(manifest, 0)

        artifacts = run_dir / "artifacts"
        if artifacts.is_dir():
            for artifact in sorted(artifacts.iterdir()):
                _section(f"{record.run_id[:8]} :: artifact {artifact.name}")
                _print_tail(artifact, lines)

        for name in (
            "phase1.final_message.md",
            "phase2.critique.md",
            "phase3.final_message.md",
            "validation-repair.final_message.md",
        ):
            candidate = run_dir / name
            if candidate.is_file():
                _section(f"{record.run_id[:8]} :: {name}")
                _print_tail(candidate, lines)

    print(f"\n{SEPARATOR}\nend of diagnostics\n{SEPARATOR}")
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI shim
    return show_logs(argv[0] if argv else None)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
