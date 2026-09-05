"""Safe lifecycle management for Claude Code's Agent Duet polling rules."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import agent_duet.claude_permissions as permissions_module
import pytest
from agent_duet.claude_permissions import (
    REQUIRED_POLL_PERMISSIONS,
    SettingsError,
    install_permissions,
    main,
    permissions_valid,
    remove_permissions,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_required_permissions_are_only_the_two_read_only_polling_tools() -> None:
    assert REQUIRED_POLL_PERMISSIONS == (
        "mcp__agent_duet__duet_status",
        "mcp__agent_duet__duet_wait",
    )
    assert "mcp__agent_duet__*" not in REQUIRED_POLL_PERMISSIONS


def test_install_creates_private_settings_with_exact_rules(tmp_path: Path) -> None:
    settings = tmp_path / ".claude/settings.json"

    assert install_permissions(settings) is True

    assert json.loads(settings.read_text()) == {
        "permissions": {"allow": list(REQUIRED_POLL_PERMISSIONS)}
    }
    assert _mode(settings) == 0o600
    assert not Path(f"{settings}.duet-backup").exists()
    assert permissions_valid(settings) is True


def test_install_preserves_existing_data_mode_and_is_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    original = {
        "permissions": {
            "allow": ["Bash(git status *)"],
            "defaultMode": "auto",
        },
        "theme": "café",
        "nested": {"keep": [1, True, None]},
    }
    original_text = json.dumps(original, ensure_ascii=False, separators=(",", ":")) + "\n"
    settings.write_text(original_text)
    settings.chmod(0o640)

    assert install_permissions(settings) is True

    backup = Path(f"{settings}.duet-backup")
    assert backup.read_text() == original_text
    assert _mode(backup) == 0o640
    assert _mode(settings) == 0o640
    updated = json.loads(settings.read_text())
    assert updated["permissions"]["allow"] == [
        "Bash(git status *)",
        *REQUIRED_POLL_PERMISSIONS,
    ]
    assert updated["permissions"]["defaultMode"] == "auto"
    assert updated["theme"] == "café"
    assert updated["nested"] == original["nested"]

    settings_bytes = settings.read_bytes()
    backup_bytes = backup.read_bytes()
    settings_mtime = settings.stat().st_mtime_ns
    backup_mtime = backup.stat().st_mtime_ns

    assert install_permissions(settings) is False
    assert settings.read_bytes() == settings_bytes
    assert backup.read_bytes() == backup_bytes
    assert settings.stat().st_mtime_ns == settings_mtime
    assert backup.stat().st_mtime_ns == backup_mtime


@pytest.mark.parametrize(
    "invalid_text",
    [
        "{",
        "[]\n",
        '{"value": NaN}\n',
        '{"value": Infinity}\n',
        '{"value": -Infinity}\n',
        '{"value": 1e400}\n',
        '{"permissions": []}\n',
        '{"permissions": {"allow": "mcp__agent_duet__duet_wait"}}\n',
        '{"permissions": {"allow": ["Read", 7]}}\n',
        '{"permissions": {"ask": "mcp__agent_duet__duet_wait"}}\n',
        '{"permissions": {"deny": ["Read", 7]}}\n',
    ],
)
def test_install_refuses_invalid_settings_without_changing_them(
    tmp_path: Path, invalid_text: str
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(invalid_text)
    settings.chmod(0o640)

    with pytest.raises(SettingsError):
        install_permissions(settings)

    assert settings.read_text() == invalid_text
    assert _mode(settings) == 0o640
    assert not Path(f"{settings}.duet-backup").exists()


def test_install_refuses_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    target = tmp_path / "real-settings.json"
    target.write_text("{}\n")
    settings = tmp_path / "settings.json"
    settings.symlink_to(target)

    with pytest.raises(SettingsError, match="symbolic link"):
        install_permissions(settings)

    assert target.read_text() == "{}\n"
    assert settings.is_symlink()


def test_install_reports_an_unusable_parent_as_a_settings_error(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("occupied\n")

    with pytest.raises(SettingsError, match="could not prepare Claude Code settings"):
        install_permissions(not_a_directory / "settings.json")


def test_install_retries_and_preserves_a_concurrent_settings_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "light"}) + "\n")
    original_atomic_write = permissions_module._atomic_write
    injected = False

    def inject_change_before_first_write(*args: object, **kwargs: object) -> None:
        nonlocal injected
        if not injected:
            settings.write_text(
                json.dumps({"theme": "light", "spinnerTipsEnabled": False}) + "\n"
            )
            injected = True
        original_atomic_write(*args, **kwargs)

    monkeypatch.setattr(permissions_module, "_atomic_write", inject_change_before_first_write)

    assert install_permissions(settings) is True
    assert json.loads(settings.read_text()) == {
        "theme": "light",
        "spinnerTipsEnabled": False,
        "permissions": {"allow": list(REQUIRED_POLL_PERMISSIONS)},
    }


def test_remove_retries_and_preserves_a_concurrent_settings_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "theme": "light",
                "permissions": {"allow": list(REQUIRED_POLL_PERMISSIONS)},
            }
        )
        + "\n"
    )
    original_atomic_write = permissions_module._atomic_write
    injected = False

    def inject_change_before_first_write(*args: object, **kwargs: object) -> None:
        nonlocal injected
        if not injected:
            settings.write_text(
                json.dumps(
                    {
                        "theme": "light",
                        "spinnerTipsEnabled": False,
                        "permissions": {"allow": list(REQUIRED_POLL_PERMISSIONS)},
                    }
                )
                + "\n"
            )
            injected = True
        original_atomic_write(*args, **kwargs)

    monkeypatch.setattr(permissions_module, "_atomic_write", inject_change_before_first_write)

    assert remove_permissions(settings) is True
    assert json.loads(settings.read_text()) == {
        "theme": "light",
        "spinnerTipsEnabled": False,
    }


def test_permissions_valid_distinguishes_missing_rules_from_invalid_json(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    assert permissions_valid(settings) is False

    settings.write_text('{"permissions": {"allow": ["mcp__agent_duet__duet_status"]}}\n')
    assert permissions_valid(settings) is False

    settings.write_text("not json\n")
    with pytest.raises(SettingsError):
        permissions_valid(settings)


@pytest.mark.parametrize(
    ("policy", "rule"),
    [
        ("ask", REQUIRED_POLL_PERMISSIONS[0]),
        ("ask", "mcp__agent_duet__*"),
        ("deny", REQUIRED_POLL_PERMISSIONS[1]),
        ("deny", "mcp__agent_duet__*"),
    ],
)
def test_permissions_valid_rejects_higher_precedence_polling_conflicts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    policy: str,
    rule: str,
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": list(REQUIRED_POLL_PERMISSIONS),
                    policy: [rule],
                }
            }
        )
        + "\n"
    )

    assert permissions_valid(settings) is False
    assert main(["check", str(settings)]) == 1
    assert f"permissions.{policy}" in capsys.readouterr().err


def test_remove_deletes_only_agent_duet_exact_rules(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    original = {
        "permissions": {
            "allow": [
                "Read",
                REQUIRED_POLL_PERMISSIONS[0],
                "mcp__agent_duet__*",
                REQUIRED_POLL_PERMISSIONS[1],
                REQUIRED_POLL_PERMISSIONS[0],
            ],
            "deny": ["Bash(rm *)"],
        },
        "theme": "dark",
    }
    settings.write_text(json.dumps(original, indent=2) + "\n")

    assert remove_permissions(settings) is True

    updated = json.loads(settings.read_text())
    assert updated == {
        "permissions": {
            "allow": ["Read", "mcp__agent_duet__*"],
            "deny": ["Bash(rm *)"],
        },
        "theme": "dark",
    }
    assert remove_permissions(settings) is False


def test_remove_prunes_empty_containers_but_keeps_the_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"permissions": {"allow": list(REQUIRED_POLL_PERMISSIONS)}}) + "\n"
    )

    assert remove_permissions(settings) is True
    assert json.loads(settings.read_text()) == {}
    assert settings.is_file()


def test_cli_check_uses_distinct_missing_and_invalid_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = tmp_path / "settings.json"

    assert main(["check", str(settings)]) == 1
    assert "polling permissions are missing" in capsys.readouterr().err

    settings.write_text("invalid\n")
    assert main(["check", str(settings)]) == 2
    assert "could not parse Claude Code settings" in capsys.readouterr().err

    settings.write_text("{}\n")
    assert main(["install", str(settings)]) == 0
    capsys.readouterr()
    assert main(["check", str(settings)]) == 0
    assert "polling permissions are configured" in capsys.readouterr().out
