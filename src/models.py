#!/usr/bin/env python3
"""Pydantic boundary models and the run state machine enum.

Every value that crosses the MCP boundary is defined here with ``extra="forbid"`` so an
unexpected field is a hard error rather than a silently ignored instruction.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class Phase(StrEnum):
    """Durable run phases. See ``BUILD_SPEC.md`` for the required transition graph."""

    QUEUED = "QUEUED"
    CLAUDE_IMPLEMENTING = "CLAUDE_IMPLEMENTING"
    HANDOFF_VALIDATING = "HANDOFF_VALIDATING"
    CODEX_REVIEWING = "CODEX_REVIEWING"
    REVIEW_INTEGRITY_CHECK = "REVIEW_INTEGRITY_CHECK"
    CLAUDE_RECONCILING = "CLAUDE_RECONCILING"
    FINAL_VALIDATING = "FINAL_VALIDATING"
    AWAITING_FINALIZE = "AWAITING_FINALIZE"
    FINALIZING = "FINALIZING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_PHASES: frozenset[Phase] = frozenset(
    {Phase.COMPLETE, Phase.FAILED, Phase.CANCELLED}
)

#: Allowed forward transitions. Any other transition is a programming error and the
#: state store refuses it, so a corrupted worker cannot fake progress.
ALLOWED_TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.QUEUED: frozenset(
        {Phase.CLAUDE_IMPLEMENTING, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.CLAUDE_IMPLEMENTING: frozenset(
        {Phase.HANDOFF_VALIDATING, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.HANDOFF_VALIDATING: frozenset(
        {Phase.CODEX_REVIEWING, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.CODEX_REVIEWING: frozenset(
        {Phase.REVIEW_INTEGRITY_CHECK, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.REVIEW_INTEGRITY_CHECK: frozenset(
        {Phase.CLAUDE_RECONCILING, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.CLAUDE_RECONCILING: frozenset(
        {Phase.FINAL_VALIDATING, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.FINAL_VALIDATING: frozenset(
        {Phase.AWAITING_FINALIZE, Phase.FAILED, Phase.CANCELLED}
    ),
    Phase.AWAITING_FINALIZE: frozenset({Phase.FINALIZING, Phase.FAILED, Phase.CANCELLED}),
    Phase.FINALIZING: frozenset({Phase.COMPLETE, Phase.FAILED}),
    Phase.COMPLETE: frozenset(),
    Phase.FAILED: frozenset(),
    Phase.CANCELLED: frozenset(),
}


def transition_allowed(current: Phase, nxt: Phase) -> bool:
    """Return whether ``current -> nxt`` is a legal state machine edge."""
    return nxt in ALLOWED_TRANSITIONS[current]


# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------

#: Git refname characters we accept. Deliberately narrower than git's own rules.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def valid_branch(name: str) -> bool:
    """Return whether ``name`` is an acceptable branch name for this coordinator."""
    if not _BRANCH_RE.match(name):
        return False
    # git's own refname prohibitions that the regex above cannot express.
    forbidden = ("..", "@{", "//", ".lock/", "/.")
    if any(token in name for token in forbidden):
        return False
    return not (name.endswith((".lock", "/", ".")) or name.startswith("-"))


# ---------------------------------------------------------------------------
# Tool inputs
# ---------------------------------------------------------------------------


class StartRequest(BaseModel):
    """Input for ``duet_start``."""

    model_config = ConfigDict(extra="forbid")

    repo_path: Path
    task: str = Field(min_length=1, max_length=50_000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=100)
    delivery_mode: Literal["review_branch", "direct_branch"] = "review_branch"
    expected_base_ref: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("acceptance_criteria")
    @classmethod
    def _criteria_bounded(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip():
                raise ValueError("acceptance criteria must not be blank")
            if len(item) > 2_000:
                raise ValueError("each acceptance criterion must be <= 2000 characters")
        return value


class FinalizeRequest(BaseModel):
    """Input for ``duet_finalize``. Every expectation here is re-verified server side."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    expected_branch: str = Field(min_length=1, max_length=200)
    expected_remote_name: str = "origin"
    expected_remote_url: str = Field(min_length=1, max_length=500)
    commit_message: str = Field(min_length=1, max_length=500)
    push: bool = True
    deployment_profile: str | None = Field(default=None, max_length=200)

    @field_validator("expected_branch")
    @classmethod
    def _branch_ok(cls, value: str) -> str:
        if not valid_branch(value):
            raise ValueError(f"unacceptable branch name: {value!r}")
        return value

    @field_validator("expected_remote_name")
    @classmethod
    def _remote_ok(cls, value: str) -> str:
        if not _REMOTE_NAME_RE.match(value):
            raise ValueError(f"unacceptable remote name: {value!r}")
        return value


# ---------------------------------------------------------------------------
# Tool outputs
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """Concise, machine-checkable evidence. Never a transcript.

    This is the *public* projection. The run row also carries private bookkeeping keys
    (``start_remotes``, for instance) that must never widen the MCP surface, so
    :meth:`from_record` selects known fields instead of validating the raw mapping.
    A stray internal key must not be able to break a status read.
    """

    model_config = ConfigDict(extra="forbid")

    validation_manifest: str | None = None
    critique_archived: bool = False
    critique_sha256: str | None = None
    critique_redacted: bool = False
    handoff_sha256: str | None = None
    working_diff_sha256: str | None = None
    validated_tree_sha: str | None = None
    changed_path_count: int | None = None
    codex_readonly_verified: bool | None = None
    codex_mutations_detected: list[str] = Field(default_factory=list)
    validations: list[ValidationResult] = Field(default_factory=list)
    #: True when the repository has no configured validation_commands, so nothing
    #: independent checked this run. Reported rather than implied by an empty
    #: ``validations`` list, which reads too easily as "all checks passed".
    unvalidated: bool = False
    proposed_commit_message: str | None = None
    commit_safety_warnings: list[str] = Field(default_factory=list)
    deployment: DeploymentEvidence | None = None
    run_dir: str | None = None
    claude_version: str | None = None
    codex_version: str | None = None
    host: str | None = None

    @classmethod
    def from_record(cls, stored: dict[str, Any]) -> Evidence:
        """Build the public evidence from a run's stored blob, ignoring private keys."""
        known = {name: stored[name] for name in cls.model_fields if name in stored}
        return cls.model_validate(known)


class ValidationResult(BaseModel):
    """One configured validation command vector and its measured outcome."""

    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    cwd: str
    exit_code: int | None
    started_at: str
    ended_at: str
    passed: bool
    log_path: str
    tail: str = ""


class RunStatus(BaseModel):
    """The single status shape returned by status/wait/start/cancel."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    phase: Phase
    terminal: bool
    repo: str
    worktree: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    current_sha: str | None = None
    delivery_mode: str = "review_branch"
    created_at: str
    updated_at: str
    summary: str = ""
    error: str | None = None
    evidence: Evidence = Field(default_factory=lambda: Evidence())
    next_action: str = ""


class DeploymentEvidence(BaseModel):
    """Deployment verifier output. Absent verifier means ``NOT_CHECKED``, never success."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["VERIFIED", "FAILED", "NOT_CHECKED"] = "NOT_CHECKED"
    deployed_sha: str | None = None
    health: str | None = None
    checked_at: str | None = None
    target: str | None = None
    detail: str = ""


class FinalizeResult(BaseModel):
    """Result of ``duet_finalize``: exact object ids, never a model claim."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    phase: Phase
    terminal: bool
    branch: str
    local_commit_sha: str
    remote_commit_sha: str | None = None
    pushed: bool = False
    remote_name: str | None = None
    remote_url: str | None = None
    staged_paths: list[str] = Field(default_factory=list)
    tree_sha: str | None = None
    validation_manifest: str | None = None
    deployment: DeploymentEvidence = Field(default_factory=lambda: DeploymentEvidence())
    summary: str = ""
    next_action: str = ""


Evidence.model_rebuild()
