#!/usr/bin/env python3
"""Atomic writes, artifact validation, bounded logs, and the pre-commit scan."""

from __future__ import annotations

import os

import pytest
from agent_duet.artifacts import (
    CRITIQUE_FILENAME,
    HANDOFF_FILENAME,
    ArtifactError,
    BoundedLog,
    archive_and_remove,
    atomic_write_text,
    read_text_bounded,
    scan_commit_set,
    sha256_text,
    tail_text,
    validate_critique,
    validate_handoff,
    write_critique,
    write_json,
)

GOOD_HANDOFF = """# Critique request

## Objective
Do the thing, satisfying the acceptance criteria.

## Files changed
- impl.py: the change

## Risks
None known.
""" + ("padding\n" * 20)

GOOD_CRITIQUE = """## Verdict
Approve.

## Confirmed issues
- low: missing docstring.

## Prioritized checklist for Claude
1. Add a docstring.
""" + ("padding\n" * 30)


def test_atomic_write_sets_mode_and_returns_digest(tmp_path):
    target = tmp_path / "x.md"
    digest = atomic_write_text(target, "hello\n")
    assert target.read_text() == "hello\n"
    assert digest == sha256_text("hello\n")
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_atomic_write_leaves_no_temp_file(tmp_path):
    atomic_write_text(tmp_path / "x.md", "hello\n")
    assert [p.name for p in tmp_path.iterdir()] == ["x.md"]


def test_atomic_write_overwrites_in_place(tmp_path):
    target = tmp_path / "x.md"
    atomic_write_text(target, "first\n")
    atomic_write_text(target, "second\n")
    assert target.read_text() == "second\n"


def test_write_json_round_trips(tmp_path):
    import json

    target = tmp_path / "x.json"
    write_json(target, {"b": 1, "a": 2})
    assert json.loads(target.read_text()) == {"a": 2, "b": 1}


def test_validate_handoff_accepts_a_good_file(tmp_path):
    (tmp_path / HANDOFF_FILENAME).write_text(GOOD_HANDOFF)
    path, digest = validate_handoff(tmp_path)
    assert path.name == HANDOFF_FILENAME
    assert digest == sha256_text(GOOD_HANDOFF)


def test_validate_handoff_requires_the_file(tmp_path):
    with pytest.raises(ArtifactError, match="was not created"):
        validate_handoff(tmp_path)


def test_validate_handoff_rejects_a_stub(tmp_path):
    (tmp_path / HANDOFF_FILENAME).write_text("done\n")
    with pytest.raises(ArtifactError, match="implausibly short"):
        validate_handoff(tmp_path)


def test_validate_handoff_requires_the_expected_headings(tmp_path):
    (tmp_path / HANDOFF_FILENAME).write_text("# Notes\n" + ("filler\n" * 40))
    with pytest.raises(ArtifactError, match="missing required content"):
        validate_handoff(tmp_path)


def test_validate_critique_accepts_a_good_report():
    validate_critique(GOOD_CRITIQUE)


def test_validate_critique_rejects_a_stub():
    with pytest.raises(ArtifactError, match="implausibly short"):
        validate_critique("Looks fine.")


def test_validate_critique_requires_sections():
    with pytest.raises(ArtifactError, match="missing required sections"):
        validate_critique("## Verdict\nApprove.\n" + ("filler\n" * 60))


def test_only_write_critique_creates_the_reviewer_file(tmp_path):
    assert not (tmp_path / CRITIQUE_FILENAME).exists()
    path, digest = write_critique(tmp_path, GOOD_CRITIQUE)
    assert path.read_text() == GOOD_CRITIQUE
    assert digest == sha256_text(GOOD_CRITIQUE)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_archive_and_remove_moves_both_artifacts(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_dir = tmp_path / "run"
    (worktree / HANDOFF_FILENAME).write_text(GOOD_HANDOFF)
    (worktree / CRITIQUE_FILENAME).write_text(GOOD_CRITIQUE)
    archived = archive_and_remove(worktree, run_dir, (HANDOFF_FILENAME, CRITIQUE_FILENAME))
    assert sorted(archived) == sorted([HANDOFF_FILENAME, CRITIQUE_FILENAME])
    assert not (worktree / HANDOFF_FILENAME).exists()
    assert not (worktree / CRITIQUE_FILENAME).exists()
    assert (run_dir / "artifacts" / HANDOFF_FILENAME).read_text() == GOOD_HANDOFF
    assert (run_dir / "artifacts" / CRITIQUE_FILENAME).read_text() == GOOD_CRITIQUE


def test_archive_and_remove_tolerates_a_missing_file(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert archive_and_remove(worktree, tmp_path / "run", (HANDOFF_FILENAME,)) == []


def test_bounded_log_stops_at_the_cap(tmp_path):
    log = BoundedLog.create(tmp_path / "out.log", max_bytes=100)
    log.append(b"a" * 60)
    log.append(b"b" * 60)
    log.append(b"c" * 60)
    body = (tmp_path / "out.log").read_bytes()
    payload, _, notice = body.partition(b"\n[TRUNCATED")
    assert payload == b"a" * 60 + b"b" * 40, "the cap is enforced byte-exactly"
    assert b"c" not in payload
    assert b"log cap reached" in notice
    assert log.truncated is True


def test_bounded_log_is_private(tmp_path):
    BoundedLog.create(tmp_path / "out.log", max_bytes=100)
    assert oct((tmp_path / "out.log").stat().st_mode & 0o777) == "0o600"


def test_read_text_bounded_marks_truncation(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text("x" * 5000)
    assert "TRUNCATED" in read_text_bounded(target, limit=1000)
    assert "TRUNCATED" not in read_text_bounded(target, limit=10_000)


def test_tail_text_redacts(tmp_path):
    target = tmp_path / "log.txt"
    target.write_text("noise\nGITHUB_TOKEN=ghp_" + "A" * 36 + "\n")
    out = tail_text(target)
    assert "ghp_" not in out
    assert "REDACTED" in out


def test_tail_text_on_a_missing_file(tmp_path):
    assert tail_text(tmp_path / "absent.log") == ""


def test_commit_scan_passes_ordinary_code(tmp_path):
    (tmp_path / "impl.py").write_text("def add(a, b):\n    return a + b\n")
    report = scan_commit_set(tmp_path, ["impl.py"])
    assert report.safe
    assert report.refusals == []


def test_commit_scan_refuses_a_credential(tmp_path):
    (tmp_path / "leak.py").write_text('TOKEN = "ghp_' + "A" * 36 + '"\n')
    report = scan_commit_set(tmp_path, ["leak.py"])
    assert not report.safe
    assert "leak.py" in report.refusals[0]


def test_commit_scan_refuses_the_coordination_artifacts(tmp_path):
    report = scan_commit_set(tmp_path, [HANDOFF_FILENAME, CRITIQUE_FILENAME])
    assert not report.safe
    assert len(report.refusals) == 2


def test_commit_scan_refuses_binaries(tmp_path):
    """Binary content is not reviewable, so it fails closed rather than warning."""
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    report = scan_commit_set(tmp_path, ["blob.bin"])
    assert not report.safe
    assert any("binary" in item for item in report.refusals)


def test_commit_scan_refuses_large_files(tmp_path):
    (tmp_path / "big.txt").write_text("a" * 6_000_000)
    report = scan_commit_set(tmp_path, ["big.txt"])
    assert not report.safe
    assert any("guard" in item for item in report.refusals)


def test_commit_scan_ignores_deleted_paths(tmp_path):
    assert scan_commit_set(tmp_path, ["was_deleted.py"]).safe


def test_commit_scan_refuses_symlinks(tmp_path):
    """A symlink's target is not reviewable content; refuse rather than warn."""
    (tmp_path / "real.txt").write_text("x\n")
    os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
    report = scan_commit_set(tmp_path, ["link.txt"])
    assert not report.safe
    assert any("symlink" in item for item in report.refusals)


def test_commit_scan_refuses_a_fifo(tmp_path):
    """Reading a FIFO can block forever, so non-regular files fail closed."""
    os.mkfifo(tmp_path / "pipe")
    report = scan_commit_set(tmp_path, ["pipe"])
    assert not report.safe
    assert any("not a regular file" in item for item in report.refusals)


def test_commit_scan_finds_a_secret_past_the_first_megabyte(tmp_path):
    """The scan streams the whole file; a prefix-only scan would miss this."""
    padding = "# pad\n" * 300_000
    (tmp_path / "big.py").write_text(padding + 'TOKEN = "ghp_' + "A" * 36 + '"\n')
    assert (tmp_path / "big.py").stat().st_size > 1 << 20
    report = scan_commit_set(tmp_path, ["big.py"])
    assert not report.safe
    assert any("credential-shaped" in item for item in report.refusals)


def test_commit_scan_matches_a_secret_across_a_chunk_boundary(tmp_path):
    """Chunks overlap, so a token straddling the read boundary is still caught."""
    token = "ghp_" + "C" * 36
    prefix = "x" * ((1 << 20) - 10)
    (tmp_path / "edge.txt").write_text(prefix + "\nTOKEN=" + token + "\n")
    report = scan_commit_set(tmp_path, ["edge.txt"])
    assert not report.safe


def test_commit_scan_warns_about_executables_without_refusing(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    report = scan_commit_set(tmp_path, ["run.sh"])
    assert report.safe
    assert any("executable" in item for item in report.warnings)
