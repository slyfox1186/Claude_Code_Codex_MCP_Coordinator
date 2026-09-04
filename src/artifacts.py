#!/usr/bin/env python3
"""Atomic artifact writes, bounded log capture, and commit-safety scanning.

The coordinator is the only writer of ``GPT_CRITIQUE_FOR_CLAUDE.md``. That single
trusted write happens here: temp file in the same directory, ``fsync``, then
``os.replace`` so a reader never sees a partial file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import Any

from .redact import redact, scan_for_secrets

#: The handoff Claude writes for the reviewer.
HANDOFF_FILENAME = "CLAUDE_CRITIQUE_REQUEST.md"
#: The critique the coordinator (never Codex) writes back into the worktree.
CRITIQUE_FILENAME = "GPT_CRITIQUE_FOR_CLAUDE.md"

#: Headings the handoff must contain before the review phase may start.
REQUIRED_HANDOFF_MARKERS: tuple[str, ...] = (
    "objective",
    "files changed",
    "risk",
)

#: Sections the reviewer report must contain to count as a real critique.
REQUIRED_CRITIQUE_MARKERS: tuple[str, ...] = (
    "verdict",
    "confirmed issues",
    "checklist",
)

#: Files larger than this are refused at commit time unless already tracked.
MAX_COMMIT_FILE_BYTES = 5_000_000


class ArtifactError(RuntimeError):
    """Raised when a required artifact is missing, malformed, or unsafe."""


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of a file's bytes."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_text(text: str) -> str:
    """Return the SHA-256 of a string's UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> str:
    """Write ``text`` to ``path`` atomically with ``mode``; return its SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ArtifactError(f"refusing to write through a symlink at {path}")
    # A unique name plus O_EXCL|O_NOFOLLOW means a pre-created symlink at the temp path
    # cannot redirect this write somewhere else.
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.agent-duet.tmp"
    fd = os.open(
        str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    # fsync the directory so the rename itself is durable.
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    os.chmod(path, mode)
    return sha256_text(text)


def write_json(path: Path, payload: Any, *, mode: int = 0o600) -> str:
    """Write ``payload`` as pretty JSON, atomically."""
    return atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=mode)


def read_text_bounded(path: Path, limit: int = 2_000_000) -> str:
    """Read at most ``limit`` bytes of a text file, marking truncation."""
    data = path.read_bytes()[: limit + 1]
    text = data.decode("utf-8", errors="replace")
    if len(data) > limit:
        return text[:limit] + "\n\n[TRUNCATED by agent_duet]\n"
    return text


def tail_text(path: Path, limit: int = 4_000) -> str:
    """Return the redacted last ``limit`` characters of a log file."""
    if not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit * 4:
            handle.seek(-limit * 4, os.SEEK_END)
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    return redact(text[-limit:])


@dataclass(slots=True)
class BoundedLog:
    """A capped, private log file for one child stream."""

    path: Path
    max_bytes: int
    written: int = 0
    truncated: bool = False

    @classmethod
    def create(cls, path: Path, max_bytes: int) -> BoundedLog:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.close(fd)
        return cls(path=path, max_bytes=max_bytes)

    def append(self, chunk: bytes) -> None:
        """Append ``chunk``, stopping cleanly once the cap is reached."""
        if self.truncated:
            return
        remaining = self.max_bytes - self.written
        if remaining <= 0:
            self._mark_truncated()
            return
        payload = chunk[:remaining]
        with self.path.open("ab") as handle:
            handle.write(payload)
        self.written += len(payload)
        if len(chunk) > remaining:
            self._mark_truncated()

    def _mark_truncated(self) -> None:
        self.truncated = True
        with self.path.open("ab") as handle:
            handle.write(b"\n[TRUNCATED by agent_duet: log cap reached]\n")


def validate_handoff(worktree: Path) -> tuple[Path, str]:
    """Require a non-empty handoff with the expected headings; return path and digest."""
    path = worktree / HANDOFF_FILENAME
    if not path.is_file():
        raise ArtifactError(
            f"{HANDOFF_FILENAME} was not created by the implementation phase"
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text.strip()) < 100:
        raise ArtifactError(f"{HANDOFF_FILENAME} is empty or implausibly short")
    lowered = text.lower()
    missing = [marker for marker in REQUIRED_HANDOFF_MARKERS if marker not in lowered]
    if missing:
        raise ArtifactError(
            f"{HANDOFF_FILENAME} is missing required content: {', '.join(missing)}"
        )
    return path, sha256_text(text)


def validate_critique(text: str) -> None:
    """Require the reviewer report to look like the requested critique."""
    if len(text.strip()) < 200:
        raise ArtifactError("the reviewer returned an empty or implausibly short report")
    lowered = text.lower()
    missing = [marker for marker in REQUIRED_CRITIQUE_MARKERS if marker not in lowered]
    if missing:
        raise ArtifactError(
            f"the reviewer report is missing required sections: {', '.join(missing)}"
        )


def write_critique(worktree: Path, text: str) -> tuple[Path, str]:
    """Perform the single trusted write of ``GPT_CRITIQUE_FOR_CLAUDE.md``."""
    path = worktree / CRITIQUE_FILENAME
    digest = atomic_write_text(path, text, mode=0o600)
    return path, digest


def archive_and_remove(worktree: Path, run_dir: Path, filenames: tuple[str, ...]) -> list[str]:
    """Copy handoff artifacts into the private run directory, then remove them.

    They must not survive into the commit set: they are coordination scratch, not
    deliverables.
    """
    archive_dir = run_dir / "artifacts"
    archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    archived: list[str] = []
    for name in filenames:
        source = worktree / name
        if not source.is_file():
            continue
        destination = archive_dir / name
        shutil.copy2(source, destination)
        destination.chmod(0o600)
        source.unlink()
        archived.append(name)
    return archived


@dataclass(slots=True)
class CommitSafetyReport:
    """What a pre-commit scan of the changed files found."""

    refusals: list[str]
    warnings: list[str]

    @property
    def safe(self) -> bool:
        return not self.refusals


_BINARY_HINT = re.compile(rb"[\x00-\x08\x0e-\x1f]")


def scan_commit_set(worktree: Path, paths: list[str]) -> CommitSafetyReport:
    """Refuse anything unfit to commit; warn about the merely unusual.

    The scan streams every byte of each eligible text file rather than sampling a
    prefix, because a credential pasted at the end of a large file is exactly as
    dangerous as one at the top. Chunks overlap so a token straddling a boundary is
    still matched. Non-regular paths (symlink, device, FIFO, socket) and oversized or
    binary files fail closed: they are refusals, not warnings, because none of them is
    something this workflow legitimately produces.
    """
    refusals: list[str] = []
    warnings: list[str] = []
    for rel in paths:
        if rel in (HANDOFF_FILENAME, CRITIQUE_FILENAME):
            refusals.append(f"{rel} is a coordination artifact and must not be committed")
            continue
        target = worktree / rel
        if target.is_symlink():
            refusals.append(
                f"{rel} is a symlink; agent_duet does not commit symlinks because their "
                "target is not reviewable content"
            )
            continue
        if not target.exists():
            continue  # A deletion; nothing to scan.
        try:
            stat = target.stat()
        except OSError as exc:
            refusals.append(f"{rel} could not be inspected: {exc}")
            continue
        if not S_ISREG(stat.st_mode):
            refusals.append(f"{rel} is not a regular file; refusing to commit it")
            continue
        if stat.st_size > MAX_COMMIT_FILE_BYTES:
            refusals.append(
                f"{rel} is {stat.st_size} bytes, over the {MAX_COMMIT_FILE_BYTES}-byte "
                "guard; commit large assets deliberately, not through an agent run"
            )
            continue

        binary, findings = _scan_file(target)
        if binary:
            refusals.append(f"{rel} contains binary content; refusing to commit it blind")
            continue
        if findings:
            refusals.append(f"{rel} contains credential-shaped content ({findings[0]})")
            continue
        if stat.st_mode & 0o111:
            warnings.append(f"{rel} is executable")
    return CommitSafetyReport(refusals=refusals, warnings=warnings)


#: Chunk size for the streaming scan, plus the overlap that keeps a token intact across
#: a boundary. The overlap comfortably exceeds the longest pattern this scanner matches.
_SCAN_CHUNK = 1 << 20
_SCAN_OVERLAP = 8_192


def _scan_file(path: Path) -> tuple[bool, list[str]]:
    """Stream ``path``; return (looks_binary, secret findings) over its entire contents."""
    findings: list[str] = []
    carry = ""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_SCAN_CHUNK)
                if not chunk:
                    break
                if _BINARY_HINT.search(chunk):
                    return True, []
                text = carry + chunk.decode("utf-8", errors="replace")
                found = scan_for_secrets(text)
                if found:
                    findings.extend(found)
                    return False, findings
                carry = text[-_SCAN_OVERLAP:]
    except OSError as exc:
        return False, [f"unreadable: {exc}"]
    return False, findings
