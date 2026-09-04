#!/usr/bin/env python3
"""Boundary models, the transition graph, and branch-name policy."""

from __future__ import annotations

import itertools

import pytest
from agent_duet.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_PHASES,
    FinalizeRequest,
    Phase,
    StartRequest,
    transition_allowed,
    valid_branch,
)
from pydantic import ValidationError


def test_start_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        StartRequest(repo_path="/tmp/x", task="do it", surprise=True)


def test_start_request_rejects_empty_task():
    with pytest.raises(ValidationError):
        StartRequest(repo_path="/tmp/x", task="")


def test_start_request_rejects_blank_criterion():
    with pytest.raises(ValidationError):
        StartRequest(repo_path="/tmp/x", task="t", acceptance_criteria=["ok", "   "])


def test_start_request_caps_criteria_count():
    with pytest.raises(ValidationError):
        StartRequest(repo_path="/tmp/x", task="t", acceptance_criteria=["x"] * 101)


def test_start_request_defaults_to_the_branch_the_user_is_already_on():
    """A run nobody configured lands on the caller's branch, not a side branch.

    Defaulting the other way meant a run could finish, leave the user's branch untouched,
    and hand them an `agent-duet/<id>` branch plus a pull request they never asked for.
    """
    request = StartRequest(repo_path="/tmp/x", task="t")
    assert request.delivery_mode == "direct_branch"


def test_finalize_request_rejects_bad_branch():
    with pytest.raises(ValidationError):
        FinalizeRequest(
            run_id="00000000-0000-4000-8000-000000000000",
            expected_branch="../evil",
            expected_remote_url="git@github.com:o/r.git",
            commit_message="m",
        )


def test_finalize_request_rejects_bad_remote_name():
    with pytest.raises(ValidationError):
        FinalizeRequest(
            run_id="00000000-0000-4000-8000-000000000000",
            expected_branch="agent-duet/abc",
            expected_remote_name="origin;rm -rf /",
            expected_remote_url="git@github.com:o/r.git",
            commit_message="m",
        )


def test_finalize_request_requires_message():
    with pytest.raises(ValidationError):
        FinalizeRequest(
            run_id="00000000-0000-4000-8000-000000000000",
            expected_branch="agent-duet/abc",
            expected_remote_url="git@github.com:o/r.git",
            commit_message="",
        )


@pytest.mark.parametrize(
    "name",
    ["main", "agent-duet/abc123", "feature/x.y", "release-1.2.3"],
)
def test_valid_branch_accepts(name):
    assert valid_branch(name)


@pytest.mark.parametrize(
    "name",
    ["", "-leading", "has space", "a..b", "ends/", "ends.", "x.lock", "a@{b", "a//b", "/.hidden"],
)
def test_valid_branch_rejects(name):
    assert not valid_branch(name)


def test_every_phase_has_a_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(Phase)


def test_terminal_phases_have_no_successors():
    for phase in TERMINAL_PHASES:
        assert ALLOWED_TRANSITIONS[phase] == frozenset()


def test_happy_path_is_reachable():
    path = [
        Phase.QUEUED,
        Phase.CLAUDE_IMPLEMENTING,
        Phase.HANDOFF_VALIDATING,
        Phase.CODEX_REVIEWING,
        Phase.REVIEW_INTEGRITY_CHECK,
        Phase.CLAUDE_RECONCILING,
        Phase.FINAL_VALIDATING,
        Phase.AWAITING_FINALIZE,
        Phase.FINALIZING,
        Phase.COMPLETE,
    ]
    for current, nxt in itertools.pairwise(path):
        assert transition_allowed(current, nxt), f"{current} -> {nxt}"


def test_cannot_skip_the_review():
    assert not transition_allowed(Phase.CLAUDE_IMPLEMENTING, Phase.CLAUDE_RECONCILING)
    assert not transition_allowed(Phase.CLAUDE_IMPLEMENTING, Phase.AWAITING_FINALIZE)


def test_cannot_finalize_without_awaiting_finalize():
    for phase in Phase:
        if phase is Phase.AWAITING_FINALIZE:
            continue
        assert not transition_allowed(phase, Phase.FINALIZING)


def test_finalizing_cannot_be_cancelled():
    assert not transition_allowed(Phase.FINALIZING, Phase.CANCELLED)
