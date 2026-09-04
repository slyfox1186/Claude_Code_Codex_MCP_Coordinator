#!/usr/bin/env python3
"""Configuration loading and validation.

The config file is the *only* source of executable paths, repository allowlists, and
command vectors. Nothing here may ever be supplied by a model, by task text, or by a
file inside the repository under test: those are untrusted inputs.

Child agents run with full machine access by explicit operator decision (see
``SECURITY.md``). The sandbox knobs remain configurable so a stricter operator can
turn them back on without a code change.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path
from stat import S_ISREG, S_IWGRP, S_IWOTH
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

APP_NAME = "agent-duet"

#: Permission modes accepted by `claude --permission-mode` in Claude Code 2.x.
ClaudePermissionMode = Literal[
    "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"
]

#: `codex exec --sandbox` values, plus "bypass" meaning
#: `--dangerously-bypass-approvals-and-sandbox` (no OS sandbox at all).
CodexSandboxMode = Literal["read-only", "workspace-write", "danger-full-access", "bypass"]


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed, or unsafe."""


def _resolve_executable(value: str, label: str) -> Path:
    """Resolve ``value`` to an absolute, existing, executable regular file."""
    if not value:
        raise ConfigError(f"[{label}] executable is required")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        found = shutil.which(value)
        if not found:
            raise ConfigError(
                f"[{label}] executable {value!r} is not absolute and was not found on PATH; "
                f"use the output of `command -v {value}`"
            )
        candidate = Path(found)
    # Deliberately absolute-but-not-resolved: these CLIs self-update, and following the
    # symlink here would pin the run to a version directory that can be deleted out from
    # under it. The target is still verified below, through the link.
    candidate = candidate.absolute()
    if not candidate.is_file():
        raise ConfigError(f"[{label}] executable {candidate} is not a regular file")
    if not os.access(candidate, os.X_OK):
        raise ConfigError(f"[{label}] executable {candidate} is not executable")
    return candidate


class ClaudeConfig(BaseModel):
    """How the coordinator invokes the two Claude Code phases."""

    model_config = ConfigDict(extra="forbid")

    executable: str
    permission_mode: ClaudePermissionMode = "bypassPermissions"
    dangerously_skip_permissions: bool = True
    #: Empty means "pass no --allowedTools flag", i.e. the full built-in toolset.
    allowed_tools: list[str] = Field(default_factory=list)
    #: Always enforced on top of whatever is configured; blocks MCP recursion.
    disallowed_tools: list[str] = Field(default_factory=lambda: ["mcp__*"])
    model: str = ""
    max_budget_usd: float = Field(default=0.0, ge=0.0)
    #: Extra directories the child may touch, beyond its working root.
    extra_dirs: list[str] = Field(default_factory=list)
    #: Escape hatch for CLI flags added after this release. Never task-supplied.
    extra_args: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=7200, ge=60, le=86_400)


class CodexConfig(BaseModel):
    """How the coordinator invokes the Codex review phase."""

    model_config = ConfigDict(extra="forbid")

    executable: str
    sandbox_mode: CodexSandboxMode = "bypass"
    ignore_user_config: bool = True
    ephemeral: bool = True
    model: str = ""
    #: "warn" records reviewer-caused repository mutations as evidence and continues;
    #: "fail" aborts the run. "fail" is only meaningful with a read-only sandbox.
    write_policy: Literal["warn", "fail"] = "warn"
    extra_args: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=7200, ge=60, le=86_400)


class GitConfig(BaseModel):
    """Branch and remote policy."""

    model_config = ConfigDict(extra="forbid")

    default_delivery_mode: Literal["review_branch", "direct_branch"] = "review_branch"
    branch_prefix: str = "agent-duet/"
    allowed_remote_names: list[str] = Field(default_factory=lambda: ["origin"])

    @field_validator("branch_prefix")
    @classmethod
    def _prefix_ok(cls, value: str) -> str:
        if not value or value.startswith("-") or ".." in value:
            raise ValueError(f"unacceptable branch_prefix: {value!r}")
        return value


class DeploymentProfile(BaseModel):
    """A fixed verifier command vector bound to one expected remote URL."""

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(min_length=1)
    expected_remote_url: str = ""
    timeout_seconds: int = Field(default=900, ge=10, le=7200)


class DeploymentConfig(BaseModel):
    """Deployment verification is opt-in and always evidence-based."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    profiles: dict[str, DeploymentProfile] = Field(default_factory=dict)


class RepositoryConfig(BaseModel):
    """Trusted, administrator-owned settings keyed by canonical repository path."""

    model_config = ConfigDict(extra="forbid")

    path: str
    validation_commands: list[list[str]] = Field(default_factory=list)
    deployment_profile: str | None = None
    validation_timeout_seconds: int = Field(default=3600, ge=10, le=86_400)

    @field_validator("validation_commands")
    @classmethod
    def _vectors_ok(cls, value: list[list[str]]) -> list[list[str]]:
        for vector in value:
            if not vector:
                raise ValueError("a validation command vector must not be empty")
            if any(not isinstance(part, str) or not part for part in vector):
                raise ValueError("validation command vectors must be non-empty strings")
        return value


class Config(BaseModel):
    """The whole ``config.toml``, strictly validated."""

    model_config = ConfigDict(extra="forbid")

    allowed_repo_roots: list[str] = Field(min_length=1)
    state_dir: str
    max_parallel_global: int = Field(default=1, ge=1, le=16)
    phase_timeout_seconds: int = Field(default=7200, ge=60, le=86_400)
    wait_max_seconds: int = Field(default=300, ge=1, le=300)
    log_max_bytes_per_stream: int = Field(default=25_000_000, ge=10_000, le=1_000_000_000)
    #: Verbosity of the coordinator's own process logs. AGENT_DUET_LOG_LEVEL overrides it.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG"
    #: "inherit" gives children the operator's full environment (default, per
    #: SECURITY.md); "minimal" passes only an allowlist.
    child_env_mode: Literal["inherit", "minimal"] = "inherit"
    claude: ClaudeConfig
    codex: CodexConfig
    git: GitConfig = Field(default_factory=GitConfig)
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    repositories: list[RepositoryConfig] = Field(default_factory=list)

    # Populated during validation; not read from the file.
    claude_path: Path = Field(default=Path("/nonexistent"), exclude=True)
    codex_path: Path = Field(default=Path("/nonexistent"), exclude=True)
    source_path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _validate_all(self) -> Config:
        roots: list[str] = []
        for raw in self.allowed_repo_roots:
            root = Path(raw).expanduser()
            if not root.is_absolute():
                raise ValueError(f"allowed_repo_roots entry must be absolute: {raw!r}")
            resolved = root.resolve()
            if resolved == Path("/") or resolved == Path.home():
                raise ValueError(
                    f"allowed_repo_roots entry is too broad: {resolved} "
                    "(the filesystem root and the home directory itself are refused)"
                )
            roots.append(str(resolved))
        object.__setattr__(self, "allowed_repo_roots", roots)

        state = Path(self.state_dir).expanduser()
        if not state.is_absolute():
            raise ValueError("state_dir must be an absolute path")
        object.__setattr__(self, "state_dir", str(state))

        seen: set[str] = set()
        for repo in self.repositories:
            resolved_repo = str(Path(repo.path).expanduser().resolve())
            if resolved_repo in seen:
                raise ValueError(f"duplicate [[repositories]] entry for {resolved_repo}")
            seen.add(resolved_repo)
            repo.path = resolved_repo
            if repo.deployment_profile and repo.deployment_profile not in (
                self.deployment.profiles
            ):
                raise ValueError(
                    f"repository {resolved_repo} names unknown deployment profile "
                    f"{repo.deployment_profile!r}"
                )
        return self

    # -- derived helpers ---------------------------------------------------

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir)

    @property
    def db_path(self) -> Path:
        return self.state_path / "state.sqlite3"

    @property
    def runs_dir(self) -> Path:
        return self.state_path / "runs"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_path / "worktrees"

    @property
    def locks_dir(self) -> Path:
        return self.state_path / "locks"

    def repository_for(self, canonical_repo: Path) -> RepositoryConfig | None:
        """Return the trusted per-repository entry for ``canonical_repo``, if any."""
        target = str(canonical_repo)
        for repo in self.repositories:
            if repo.path == target:
                return repo
        return None

    def root_allows(self, canonical_repo: Path) -> bool:
        """Return whether ``canonical_repo`` lies strictly below an allowlisted root."""
        for root in self.allowed_repo_roots:
            root_path = Path(root)
            if canonical_repo == root_path:
                return False  # The root itself is never a run target.
            if root_path in canonical_repo.parents:
                return True
        return False

    def resolve_executables(self) -> None:
        """Resolve and record both CLI paths, raising :class:`ConfigError` on failure."""
        object.__setattr__(
            self, "claude_path", _resolve_executable(self.claude.executable, "claude")
        )
        object.__setattr__(
            self, "codex_path", _resolve_executable(self.codex.executable, "codex")
        )


def _require_trustworthy_config_file(target: Path) -> None:
    """Refuse a config file anyone else could have written.

    This file names the executables to run and the exact command vectors the
    coordinator will execute, so write access to it is equivalent to code execution.
    A symlink, foreign ownership, or group/world write permission is fatal.
    """
    if target.is_symlink():
        raise ConfigError(
            f"{target} is a symlink; agent-duet refuses to read configuration through "
            "a link because the target could be swapped underneath it"
        )
    try:
        info = target.stat()
    except OSError as exc:
        raise ConfigError(f"could not stat {target}: {exc}") from exc
    if not S_ISREG(info.st_mode):
        raise ConfigError(f"{target} is not a regular file")
    if info.st_uid != os.getuid():
        raise ConfigError(
            f"{target} is owned by uid {info.st_uid}, not by you (uid {os.getuid()}); "
            "refusing to trust configuration you do not own"
        )
    if info.st_mode & (S_IWGRP | S_IWOTH):
        raise ConfigError(
            f"{target} is group- or world-writable (mode {oct(info.st_mode & 0o777)}); "
            f"run: chmod 600 {target}"
        )


def config_path() -> Path:
    """Return the config file location, honouring ``XDG_CONFIG_HOME``."""
    override = os.environ.get("AGENT_DUET_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP_NAME / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Load, validate, and return the configuration.

    ``.env`` files are deliberately never consulted.
    """
    target = path or config_path()
    if not target.is_file():
        raise ConfigError(
            f"configuration not found at {target}; copy config.example.toml there, "
            "replace the placeholders, and chmod 600 it"
        )
    _require_trustworthy_config_file(target)
    try:
        with target.open("rb") as handle:
            raw: dict[str, Any] = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{target} is not valid TOML: {exc}") from exc

    try:
        config = Config.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"{target} failed validation: {exc}") from exc

    object.__setattr__(config, "source_path", target)
    config.resolve_executables()
    return config


def ensure_state_dirs(config: Config) -> None:
    """Create the state tree with private permissions (0700 dirs)."""
    for directory in (
        config.state_path,
        config.runs_dir,
        config.worktrees_dir,
        config.locks_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir's mode is subject to umask; enforce it explicitly.
        directory.chmod(0o700)
