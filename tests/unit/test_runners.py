#!/usr/bin/env python3
"""Command-vector construction and child output parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_duet.config import ClaudeConfig, CodexConfig
from agent_duet.runners import (
    STDIN_POINTER,
    RunnerError,
    build_claude_argv,
    build_codex_argv,
    parse_claude_output,
    parse_codex_events,
)

from helpers import FIXTURE_BIN


@pytest.fixture
def claude_cfg() -> ClaudeConfig:
    return ClaudeConfig(executable=str(FIXTURE_BIN / "fake-claude"))


@pytest.fixture
def codex_cfg() -> CodexConfig:
    return CodexConfig(executable=str(FIXTURE_BIN / "fake-codex"))


# -- claude argv -----------------------------------------------------------


def test_claude_argv_is_non_interactive_and_json(claude_cfg, tmp_path):
    argv = build_claude_argv(claude_cfg, Path("/bin/claude"), tmp_path)
    assert argv[0] == "/bin/claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"


def test_claude_argv_points_the_prompt_at_stdin(claude_cfg, tmp_path):
    argv = build_claude_argv(claude_cfg, Path("/bin/claude"), tmp_path)
    assert argv[argv.index("-p") + 1] == STDIN_POINTER


def test_claude_argv_isolates_mcp(claude_cfg, tmp_path):
    argv = build_claude_argv(claude_cfg, Path("/bin/claude"), tmp_path)
    assert "--strict-mcp-config" in argv
    config = Path(argv[argv.index("--mcp-config") + 1])
    assert json.loads(config.read_text()) == {"mcpServers": {}}
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__*"


def test_claude_argv_forces_the_mcp_denial_even_if_configured_away(tmp_path):
    cfg = ClaudeConfig(executable=str(FIXTURE_BIN / "fake-claude"), disallowed_tools=["Bash"])
    argv = build_claude_argv(cfg, Path("/bin/claude"), tmp_path)
    index = argv.index("--disallowedTools")
    assert "mcp__*" in argv[index + 1 : index + 3]


def test_claude_argv_disables_session_persistence(claude_cfg, tmp_path):
    assert "--no-session-persistence" in build_claude_argv(
        claude_cfg, Path("/bin/claude"), tmp_path
    )


def test_claude_argv_full_access_by_default(claude_cfg, tmp_path):
    argv = build_claude_argv(claude_cfg, Path("/bin/claude"), tmp_path)
    assert "--dangerously-skip-permissions" in argv
    assert "--allowedTools" not in argv, "empty allowed_tools means the full toolset"


def test_claude_argv_sets_the_configured_effort(claude_cfg, tmp_path):
    argv = build_claude_argv(claude_cfg, Path("/bin/claude"), tmp_path)
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_claude_argv_honours_a_restricted_posture(tmp_path):
    cfg = ClaudeConfig(
        executable=str(FIXTURE_BIN / "fake-claude"),
        dangerously_skip_permissions=False,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Edit"],
    )
    argv = build_claude_argv(cfg, Path("/bin/claude"), tmp_path)
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    index = argv.index("--allowedTools")
    assert argv[index + 1 : index + 3] == ["Read", "Edit"]


def test_claude_argv_omits_optional_flags_when_unset(claude_cfg, tmp_path):
    argv = build_claude_argv(claude_cfg, Path("/bin/claude"), tmp_path)
    assert "--model" not in argv
    assert "--max-budget-usd" not in argv
    assert "--add-dir" not in argv


def test_claude_argv_includes_optional_flags_when_set(tmp_path):
    cfg = ClaudeConfig(
        executable=str(FIXTURE_BIN / "fake-claude"),
        model="opus",
        max_budget_usd=2.5,
        extra_dirs=["/srv/shared"],
        extra_args=["--verbose"],
    )
    argv = build_claude_argv(cfg, Path("/bin/claude"), tmp_path)
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5"
    assert argv[argv.index("--add-dir") + 1] == "/srv/shared"
    assert argv[-1] == "--verbose"


def test_claude_argv_never_carries_task_text(claude_cfg, tmp_path):
    """Dynamic content goes on stdin, never into a process listing."""
    argv = build_claude_argv(claude_cfg, Path("/bin/claude"), tmp_path)
    joined = " ".join(argv)
    assert "TASK-SECRET-MARKER" not in joined


def test_claude_argv_does_not_use_removed_flags(claude_cfg, tmp_path):
    """Claude Code 2.1.x removed these; passing them would abort every run."""
    argv = build_claude_argv(claude_cfg, Path("/bin/claude"), tmp_path)
    assert "--max-turns" not in argv
    assert "--permission-prompts" not in argv


# -- codex argv ------------------------------------------------------------


def test_codex_argv_uses_exec_and_stdin(codex_cfg, tmp_path):
    argv = build_codex_argv(codex_cfg, Path("/bin/codex"), tmp_path, tmp_path / "last.md")
    assert argv[:2] == ["/bin/codex", "exec"]
    assert argv[-1] == "-", "a bare dash makes stdin the entire prompt"


def test_codex_argv_full_access_by_default(codex_cfg, tmp_path):
    argv = build_codex_argv(codex_cfg, Path("/bin/codex"), tmp_path, tmp_path / "last.md")
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--sandbox" not in argv


def test_codex_argv_honours_a_read_only_posture(tmp_path):
    cfg = CodexConfig(executable=str(FIXTURE_BIN / "fake-codex"), sandbox_mode="read-only")
    argv = build_codex_argv(cfg, Path("/bin/codex"), tmp_path, tmp_path / "last.md")
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_codex_argv_isolates_mcp_and_user_config(codex_cfg, tmp_path):
    """--ignore-user-config is the isolation. The reviewer never sees a user config, so
    there is no agent_duet server entry to reach and none to switch off."""
    argv = build_codex_argv(codex_cfg, Path("/bin/codex"), tmp_path, tmp_path / "last.md")
    assert "--ignore-user-config" in argv
    assert "mcp_servers.agent_duet.enabled=false" not in argv


def test_codex_argv_preserves_reasoning_effort_when_user_config_is_ignored(
    codex_cfg, tmp_path
):
    argv = build_codex_argv(codex_cfg, Path("/bin/codex"), tmp_path, tmp_path / "last.md")
    overrides = [argv[index + 1] for index, item in enumerate(argv) if item == "-c"]
    assert 'model_reasoning_effort="high"' in overrides


def test_codex_argv_is_ephemeral_and_json(codex_cfg, tmp_path):
    argv = build_codex_argv(codex_cfg, Path("/bin/codex"), tmp_path, tmp_path / "last.md")
    assert "--ephemeral" in argv
    assert "--json" in argv


def test_codex_argv_sets_the_working_root_and_output_file(codex_cfg, tmp_path):
    last = tmp_path / "last.md"
    argv = build_codex_argv(codex_cfg, Path("/bin/codex"), tmp_path, last)
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[argv.index("--output-last-message") + 1] == str(last)


def test_codex_argv_does_not_use_removed_flags(codex_cfg, tmp_path):
    """`codex exec` 0.153.x has no --ask-for-approval; passing it aborts the run."""
    argv = build_codex_argv(codex_cfg, Path("/bin/codex"), tmp_path, tmp_path / "last.md")
    assert "--ask-for-approval" not in argv


# -- claude output parsing -------------------------------------------------


def test_parse_claude_object():
    payload, message = parse_claude_output(
        json.dumps({"type": "result", "is_error": False, "result": "all done"})
    )
    assert payload["type"] == "result"
    assert message == "all done"


def test_parse_claude_array_takes_the_last_message():
    text = json.dumps([{"result": "first"}, {"result": "last"}])
    _, message = parse_claude_output(text)
    assert message == "last"


def test_parse_claude_tolerates_leading_noise():
    _, message = parse_claude_output('warning: something\n{"result": "ok"}')
    assert message == "ok"


def test_parse_claude_rejects_empty_output():
    with pytest.raises(RunnerError, match="no stdout"):
        parse_claude_output("   ")


def test_parse_claude_rejects_non_json():
    with pytest.raises(RunnerError, match="not valid JSON"):
        parse_claude_output("this is plain text")


def test_parse_claude_surfaces_an_error_result():
    with pytest.raises(RunnerError, match="error result"):
        parse_claude_output(json.dumps({"is_error": True, "result": "it broke"}))


def test_parse_claude_redacts_in_the_error_message():
    with pytest.raises(RunnerError) as excinfo:
        parse_claude_output(
            json.dumps({"is_error": True, "result": "token ghp_" + "A" * 36})
        )
    assert "ghp_" not in str(excinfo.value)


# -- codex output parsing --------------------------------------------------


def test_parse_codex_finds_the_message_and_completion():
    lines = [
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "critique"}}),
        json.dumps({"type": "turn.completed"}),
    ]
    events, message, completed = parse_codex_events("\n".join(lines))
    assert len(events) == 3
    assert message == "critique"
    assert completed is True


def test_parse_codex_detects_an_incomplete_turn():
    lines = [json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "x"}})]
    _, _, completed = parse_codex_events("\n".join(lines))
    assert completed is False


def test_parse_codex_handles_the_msg_envelope_shape():
    line = json.dumps({"id": "1", "msg": {"type": "agent_message", "message": "older shape"}})
    _, message, _ = parse_codex_events(line + "\n" + json.dumps({"msg": {"type": "task_complete"}}))
    assert message == "older shape"
    

def test_parse_codex_handles_content_arrays():
    line = json.dumps(
        {"type": "assistant_message", "content": [{"text": "part one "}, {"text": "part two"}]}
    )
    _, message, _ = parse_codex_events(line)
    assert message == "part one part two"


def test_parse_codex_ignores_non_json_lines():
    events, message, completed = parse_codex_events(
        "starting up\n" + json.dumps({"type": "turn.completed"}) + "\nbye\n"
    )
    assert len(events) == 1
    assert completed is True
    assert message == ""


def test_parse_codex_takes_the_last_message():
    lines = [
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "second"}}),
        json.dumps({"type": "turn.completed"}),
    ]
    _, message, _ = parse_codex_events("\n".join(lines))
    assert message == "second"


def test_parse_codex_on_empty_input():
    events, message, completed = parse_codex_events("")
    assert events == [] and message == "" and completed is False


# ---------------------------------------------------------------------------
# Regression: the MCP-disable override is only valid alongside a loaded user config
# ---------------------------------------------------------------------------


def test_no_mcp_override_when_the_user_config_is_ignored():
    """`-c mcp_servers.agent_duet.enabled=false` with --ignore-user-config creates a
    server table with no transport, and codex refuses to start: "invalid transport"."""
    cfg = CodexConfig(executable="/bin/true", ignore_user_config=True)
    argv = build_codex_argv(cfg, Path("/bin/true"), Path("/tmp/wt"), Path("/tmp/last.md"))
    assert "--ignore-user-config" in argv
    assert "mcp_servers.agent_duet.enabled=false" not in argv


def test_mcp_override_is_used_when_the_user_config_is_loaded():
    """With a user config there IS a table to merge into, so the override is correct."""
    cfg = CodexConfig(executable="/bin/true", ignore_user_config=False)
    argv = build_codex_argv(cfg, Path("/bin/true"), Path("/tmp/wt"), Path("/tmp/last.md"))
    assert "--ignore-user-config" not in argv
    assert "mcp_servers.agent_duet.enabled=false" in argv
