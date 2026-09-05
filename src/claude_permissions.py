"""Manage the exact Claude Code permissions needed for read-only Duet polling."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

REQUIRED_POLL_PERMISSIONS = (
    "mcp__agent_duet__duet_status",
    "mcp__agent_duet__duet_wait",
)


class SettingsError(RuntimeError):
    """Claude Code settings cannot be safely inspected or changed."""


def _load_settings(path: Path) -> tuple[dict[str, Any], bool]:
    if path.is_symlink():
        raise SettingsError(f"refusing symbolic link for Claude Code settings: {path}")
    if not path.exists():
        return {}, False
    if not path.is_file():
        raise SettingsError(f"Claude Code settings path is not a regular file: {path}")
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"could not parse Claude Code settings at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SettingsError(f"Claude Code settings root must be a JSON object: {path}")
    return cast(dict[str, Any], raw), True


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


def _atomic_write(path: Path, data: dict[str, Any], *, existed: bool) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if existed else 0o600
    if existed:
        backup = _backup_path(path)
        if backup.is_symlink() or (backup.exists() and not backup.is_file()):
            raise SettingsError(f"refusing unsafe Claude Code settings backup path: {backup}")
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            raise SettingsError(
                f"could not back up Claude Code settings to {backup}: {exc}"
            ) from exc

    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temporary: Path | None = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
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
    data, existed = _load_settings(path)
    allow = _allow_list(data, create=True)
    assert allow is not None
    missing = [rule for rule in REQUIRED_POLL_PERMISSIONS if rule not in allow]
    if not missing:
        return False
    allow.extend(missing)
    _atomic_write(path, data, existed=existed)
    return True


def permissions_valid(path: Path) -> bool:
    """Return whether both exact polling rules are present in valid settings."""
    data, existed = _load_settings(path)
    if not existed:
        return False
    allow = _allow_list(data, create=False)
    return allow is not None and all(rule in allow for rule in REQUIRED_POLL_PERMISSIONS)


def remove_permissions(path: Path) -> bool:
    """Remove only Agent Duet's exact polling rules, preserving all other settings."""
    data, existed = _load_settings(path)
    if not existed:
        return False
    allow = _allow_list(data, create=False)
    if allow is None or not any(rule in allow for rule in REQUIRED_POLL_PERMISSIONS):
        return False

    permissions = cast(dict[str, Any], data["permissions"])
    permissions["allow"] = [
        rule for rule in allow if rule not in REQUIRED_POLL_PERMISSIONS
    ]
    if not permissions["allow"]:
        permissions.pop("allow")
    if not permissions:
        data.pop("permissions")
    _atomic_write(path, data, existed=True)
    return True


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
        if permissions_valid(args.settings_path):
            print(f"Claude polling permissions are configured: {args.settings_path}")
            return 0
        print(
            f"Claude polling permissions are missing: {args.settings_path}",
            file=sys.stderr,
        )
        return 1
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
