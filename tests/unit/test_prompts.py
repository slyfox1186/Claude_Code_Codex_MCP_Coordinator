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
            or not isinstance(inner.func, ast.Name)
            or inner.func.id != "_load_template"
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
