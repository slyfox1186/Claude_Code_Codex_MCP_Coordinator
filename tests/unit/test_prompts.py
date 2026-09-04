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
    one that happened to get it wrong."""
    text = (PROMPTS / name).read_text()
    assert "Root every filesystem search" in text
    assert "{timeout_minutes} minutes for this phase" in text


def test_the_phase_deadline_comes_from_the_configured_timeout():
    """The number in the prompt has to be the number the coordinator actually enforces."""
    source = (SRC / "worker.py").read_text()
    assert source.count("timeout_minutes=self.config.claude.timeout_seconds // 60") == 2
    assert source.count("timeout_minutes=self.config.codex.timeout_seconds // 60") == 1


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
