"""Manage the exact Claude Code permissions needed for read-only Duet polling."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

REQUIRED_POLL_PERMISSIONS = (
    "mcp__agent_duet__duet_status",
    "mcp__agent_duet__duet_wait",
)
MAX_UPDATE_ATTEMPTS = 5


class SettingsError(RuntimeError):
    """Claude Code settings cannot be safely inspected or changed."""


class _ConcurrentSettingsChange(RuntimeError):
    """The settings snapshot changed before its replacement could be committed."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _reject_nonfinite_numbers(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite JSON number {value}")
    if isinstance(value, list):
        for item in value:
            _reject_nonfinite_numbers(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite_numbers(item)


def _load_settings(path: Path) -> tuple[dict[str, Any], bytes | None, int]:
    descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}, None, 0o600
    except OSError as exc:
        if path.is_symlink():
            raise SettingsError(
                f"refusing symbolic link for Claude Code settings: {path}"
            ) from exc
        raise SettingsError(f"could not inspect Claude Code settings at {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SettingsError(f"Claude Code settings path is not a regular file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            snapshot = handle.read()
        raw: object = json.loads(
            snapshot.decode("utf-8"), parse_constant=_reject_json_constant
        )
        _reject_nonfinite_numbers(raw)
    except SettingsError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SettingsError(f"could not parse Claude Code settings at {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
    if not isinstance(raw, dict):
        raise SettingsError(f"Claude Code settings root must be a JSON object: {path}")
    return cast(dict[str, Any], raw), snapshot, stat.S_IMODE(metadata.st_mode)


def _allow_list(data: dict[str, Any], *, create: bool) -> list[str] | None:
    if "permissions" not in data:
        if not create:
            return None
        data["permissions"] = {}
    permissions = data["permissions"]
    if not isinstance(permissions, dict):
        raise SettingsError("Claude Code settings `permissions` must be a JSON object")

    typed_permissions = cast(dict[str, Any], permissions)
    if "allow" not in typed_permissions:
        if not create:
            return None
        typed_permissions["allow"] = []
    allow = typed_permissions["allow"]
    if not isinstance(allow, list) or not all(isinstance(rule, str) for rule in allow):
        raise SettingsError(
            "Claude Code settings `permissions.allow` must be an array of strings"
        )
    return cast(list[str], allow)


def _backup_path(path: Path) -> Path:
    return Path(f"{path}.duet-backup")


@contextmanager
def _exclusive_settings_lock(path: Path) -> Iterator[None]:
    descriptor = -1
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
        descriptor = os.open(path.parent, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise SettingsError(
                f"Claude Code settings parent is not a directory: {path.parent}"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except (OSError, SettingsError) as exc:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if isinstance(exc, SettingsError):
            raise
        raise SettingsError(f"could not prepare Claude Code settings at {path}: {exc}") from exc
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _snapshot_matches(path: Path, expected: bytes | None) -> bool:
    descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return expected is None
    except OSError as exc:
        if path.is_symlink():
            return False
        raise SettingsError(
            f"could not recheck Claude Code settings at {path}: {exc}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return expected is not None and handle.read() == expected
    except OSError as exc:
        raise SettingsError(f"could not recheck Claude Code settings at {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _write_backup(path: Path, snapshot: bytes, mode: int) -> None:
    backup = _backup_path(path)
    if backup.is_symlink() or (backup.exists() and not backup.is_file()):
        raise SettingsError(f"refusing unsafe Claude Code settings backup path: {backup}")
    temporary: Path | None = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{backup.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(snapshot)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, backup)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise SettingsError(
            f"could not back up Claude Code settings to {backup}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _atomic_write(
    path: Path,
    data: dict[str, Any],
    *,
    expected: bytes | None,
    mode: int,
) -> None:
    try:
        encoded = (
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"could not encode Claude Code settings at {path}: {exc}") from exc

    if not _snapshot_matches(path, expected):
        raise _ConcurrentSettingsChange
    if expected is not None:
        _write_backup(path, expected, mode)
        if not _snapshot_matches(path, expected):
            raise _ConcurrentSettingsChange

    temporary: Path | None = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        if not _snapshot_matches(path, expected):
            raise _ConcurrentSettingsChange
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except _ConcurrentSettingsChange:
        raise
    except OSError as exc:
        raise SettingsError(f"could not write Claude Code settings at {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def install_permissions(path: Path) -> bool:
    """Add the two exact polling rules, returning whether the file changed."""
    with _exclusive_settings_lock(path):
        for _ in range(MAX_UPDATE_ATTEMPTS):
            data, snapshot, mode = _load_settings(path)
            allow = _allow_list(data, create=True)
            assert allow is not None
            missing = [rule for rule in REQUIRED_POLL_PERMISSIONS if rule not in allow]
            if not missing:
                return False
            allow.extend(missing)
            try:
                _atomic_write(path, data, expected=snapshot, mode=mode)
            except _ConcurrentSettingsChange:
                continue
            return True
    raise SettingsError(f"Claude Code settings changed repeatedly while updating: {path}")


def _matches_permission_rule(rule: str, tool: str) -> bool:
    expression = re.escape(rule).replace(r"\*", ".*")
    return re.fullmatch(expression, tool) is not None


def _permission_problems(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    allow = _allow_list(data, create=False)
    missing = [
        rule for rule in REQUIRED_POLL_PERMISSIONS if allow is None or rule not in allow
    ]
    conflicts: list[str] = []
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return missing, conflicts
    for policy in ("deny", "ask"):
        rules = permissions.get(policy, [])
        if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
            raise SettingsError(
                f"Claude Code settings `permissions.{policy}` must be an array of strings"
            )
        for rule in cast(list[str], rules):
            matches = [
                tool for tool in REQUIRED_POLL_PERMISSIONS if _matches_permission_rule(rule, tool)
            ]
            if matches:
                conflicts.append(
                    f"permissions.{policy} rule {rule!r} matches {', '.join(matches)}"
                )
    return missing, conflicts


def permissions_valid(path: Path) -> bool:
    """Return whether both exact polling rules are present in valid settings."""
    data, snapshot, _ = _load_settings(path)
    if snapshot is None:
        return False
    missing, conflicts = _permission_problems(data)
    return not missing and not conflicts


def remove_permissions(path: Path) -> bool:
    """Remove only Agent Duet's exact polling rules, preserving all other settings."""
    _, initial_snapshot, _ = _load_settings(path)
    if initial_snapshot is None:
        return False
    with _exclusive_settings_lock(path):
        for _ in range(MAX_UPDATE_ATTEMPTS):
            data, snapshot, mode = _load_settings(path)
            if snapshot is None:
                return False
            allow = _allow_list(data, create=False)
            if allow is None or not any(
                rule in allow for rule in REQUIRED_POLL_PERMISSIONS
            ):
                return False

            permissions = cast(dict[str, Any], data["permissions"])
            permissions["allow"] = [
                rule for rule in allow if rule not in REQUIRED_POLL_PERMISSIONS
            ]
            if not permissions["allow"]:
                permissions.pop("allow")
            if not permissions:
                data.pop("permissions")
            try:
                _atomic_write(path, data, expected=snapshot, mode=mode)
            except _ConcurrentSettingsChange:
                continue
            return True
    raise SettingsError(f"Claude Code settings changed repeatedly while updating: {path}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Claude Code permissions for Agent Duet polling."
    )
    parser.add_argument("action", choices=("install", "check", "remove"))
    parser.add_argument("settings_path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "install":
            changed = install_permissions(args.settings_path)
            state = "installed" if changed else "already configured"
            print(f"Claude polling permissions are {state}: {args.settings_path}")
            return 0
        if args.action == "remove":
            changed = remove_permissions(args.settings_path)
            state = "removed" if changed else "already absent"
            print(f"Claude polling permissions are {state}: {args.settings_path}")
            return 0
        data, snapshot, _ = _load_settings(args.settings_path)
        missing, conflicts = (
            (list(REQUIRED_POLL_PERMISSIONS), [])
            if snapshot is None
            else _permission_problems(data)
        )
        if not missing and not conflicts:
            print(f"Claude polling permissions are configured: {args.settings_path}")
            return 0
        if missing:
            print(
                f"Claude polling permissions are missing at {args.settings_path}: "
                f"{', '.join(missing)}",
                file=sys.stderr,
            )
        for conflict in conflicts:
            print(
                f"Claude polling permission conflict at {args.settings_path}: {conflict}; "
                "higher-precedence ask/deny rules remain authoritative (inspect /permissions)",
                file=sys.stderr,
            )
        return 1
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
