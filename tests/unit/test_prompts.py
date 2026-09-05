#!/usr/bin/env python3
"""The child prompts are an interface between worker.py and three separate CLIs.

They are rendered with ``str.format``, so a placeholder the call site does not supply
raises ``KeyError`` in the middle of a live run -- after a worktree exists and a phase
has already been entered. These tests keep the templates and their call sites in step.
"""

from __future__ import annotations

import ast
from pathlib import Path
from string import Formatter

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
ROOT = SRC.parent
PROMPTS = SRC / "prompts"
TEMPLATES = ("claude_implement.md", "codex_review.md", "claude_reconcile.md")


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


def _call_site_keys() -> dict[str, set[str]]:
    """Map each template name to the keyword arguments worker.py renders it with.

    Read out of the AST rather than hard-coded here, so this cannot drift into agreeing
    with a stale copy of the truth.
    """
    tree = ast.parse((SRC / "worker.py").read_text())
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "format":
            continue
        inner = node.func.value
        if (
            not isinstance(inner, ast.Call)
            or not isinstance(inner.func, ast.Attribute)
            or inner.func.attr != "_template"
            or not inner.args
            or not isinstance(inner.args[0], ast.Constant)
        ):
            continue
        found[str(inner.args[0].value)] = {kw.arg for kw in node.keywords if kw.arg}
    return found


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_placeholder_is_supplied_by_its_call_site(name):
    supplied = _call_site_keys()
    assert name in supplied, f"worker.py never renders {name}"
    missing = _placeholders((PROMPTS / name).read_text()) - supplied[name]
    assert not missing, f"{name} uses placeholders worker.py does not pass: {sorted(missing)}"


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_template_actually_renders(name):
    keys = _placeholders((PROMPTS / name).read_text())
    rendered = (PROMPTS / name).read_text().format(**dict.fromkeys(keys, "X"))
    assert "{" not in rendered.replace("{{", "").replace("}}", "")


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_child_is_told_to_keep_searches_inside_its_working_root(name):
    """A phase-1 agent once ran ``find /``, which crossed a 16 TB external mount and
    stalled the run for tens of minutes. Scoping is stated in every phase, not just the
    one that happened to get it wrong.

    Asserted by substance rather than by sentence, so the wording stays editable: the
    prompt has to scope searches to the working root, warn what an unscoped one costs,
    and state the deadline. Pinning the exact phrasing only makes rewrites fail.
    """
    text = (PROMPTS / name).read_text()
    assert "Root every filesystem search" in text
    assert "$HOME" in text, "must name the roots that go wrong, not just say 'be careful'"
    assert "mounted volume" in text, "must say why an unscoped search is expensive"
    assert "{timeout_description}" in text, "must tell the phase its own deadline"


def test_the_phase_deadline_comes_from_the_configured_timeout():
    """The number in the prompt has to be the number the coordinator actually enforces."""
    source = (SRC / "worker.py").read_text()
    assert source.count("timeout_description=self._phase_timeout_description(") == 3
    assert source.count("timeout_seconds=phase_timeout") == 3


def test_phase_deadline_uses_the_tighter_global_safety_ceiling(config, store):
    from agent_duet.worker import Worker

    configured = config.model_copy(update={"phase_timeout_seconds": 90})
    worker = Worker(configured, store, "unused-run-id")
    assert worker._phase_timeout_seconds(7200) == 90
    assert worker._phase_timeout_description(7200) == "90 seconds"


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_child_is_told_to_work_deliberately_until_its_role_is_complete(name):
    text = " ".join((PROMPTS / name).read_text().lower().split())
    assert "work systematically" in text
    assert "first plausible" in text


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_child_is_forbidden_from_changing_branches(name):
    text = " ".join((PROMPTS / name).read_text().lower().split())
    assert "do not create, switch, rename, or delete branches" in text
    assert "{branch}" in text


@pytest.mark.parametrize(
    ("name", "placeholders"),
    [
        (
            "claude_implement.md",
            (
                "worktree",
                "repo_path",
                "branch",
                "base_sha",
                "upstream",
                "delivery_mode",
                "starting_status",
                "timeout_description",
            ),
        ),
        (
            "codex_review.md",
            ("worktree", "base_sha", "current_sha", "branch", "timeout_description"),
        ),
        (
            "claude_reconcile.md",
            ("worktree", "repo_path", "branch", "base_sha", "current_sha", "timeout_description"),
        ),
    ],
)
def test_authoritative_prompt_values_are_formatted_as_literals(name, placeholders):
    text = (PROMPTS / name).read_text()
    for placeholder in placeholders:
        assert f"`{{{placeholder}}}`" in text


def test_duet_command_keeps_only_one_foreground_safe_wait_in_flight():
    text = (ROOT / "commands" / "duet.md").read_text()
    assert "timeout_seconds=90" in text
    assert "exactly one `duet_wait` call in flight" in text
    assert "background task" in text


def test_duet_command_translates_internal_states_into_numbered_progress():
    text = (ROOT / "commands" / "duet.md").read_text()
    assert "Phase 1 of 3" in text
    assert "Phase 2 of 3" in text
    assert "Phase 3 of 3" in text
    assert "not the final step" in text
    assert "using existing branch" in text
    assert "no new branch" in text
    assert "Never report a raw phase enum by itself" in text


def test_server_instructions_keep_the_whole_critical_contract_in_512_characters():
    from agent_duet.server import INSTRUCTIONS

    assert len(INSTRUCTIONS) <= 512
    assert "exactly one `duet_wait`" in INSTRUCTIONS
    assert "user approval before `duet_finalize`" in INSTRUCTIONS
    assert "Never claim commit, push, deploy, or success" in INSTRUCTIONS


def test_a_run_keeps_the_templates_it_started_with(tmp_path, monkeypatch):
    """Upgrading agent-duet mid-run must not change a running worker's prompts.

    A worker outlives the session that launched it, and the templates used to be read
    from the installed package at the moment each phase began. Editing a template while
    a run was in flight handed the worker a template its own code never matched: phase 1
    and the Codex review both finished, then reconciliation died on
    ``KeyError: 'timeout_minutes'`` -- 35 minutes of completed agent work thrown away.
    """
    from agent_duet import worker as worker_module

    package_prompts = tmp_path / "package"
    package_prompts.mkdir()
    for name in worker_module.TEMPLATE_NAMES:
        (package_prompts / name).write_text("original {worktree}")
    monkeypatch.setattr(worker_module, "PROMPTS_DIR", package_prompts)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pinned = worker_module._pin_templates(run_dir)
    assert (pinned / "claude_implement.md").read_text() == "original {worktree}"

    # The package is upgraded underneath the running worker.
    for name in worker_module.TEMPLATE_NAMES:
        (package_prompts / name).write_text("upgraded {worktree} {brand_new_placeholder}")

    again = worker_module._pin_templates(run_dir)
    assert (again / "claude_implement.md").read_text() == "original {worktree}", (
        "the run must keep the templates it started with"
    )
    # And a run started after the upgrade gets the new ones.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert "brand_new_placeholder" in (
        worker_module._pin_templates(fresh) / "claude_implement.md"
    ).read_text()
