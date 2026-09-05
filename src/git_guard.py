#!/usr/bin/env python3
"""Repository validation, locking, fingerprinting, and the only code that writes to git.

Two rules drive this module:

* every claim about the repository is a measured git object id or porcelain record,
  never something a model reported;
* exactly one writer at a time per repository, keyed by the canonical git *common*
  directory so worktrees of the same repository share a lock.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("agent_duet.git")

GIT = "git"

#: Timeout for every git invocation. Git is local and fast; a hang means trouble.
GIT_TIMEOUT = 300


class GitError(RuntimeError):
    """Raised when a git command fails or the repository violates an invariant."""


class RepoLockedError(GitError):
    """Raised when another agent_duet writer already holds this repository."""


@dataclass(slots=True)
class GitResult:
    """One completed git invocation."""

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def run_git(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    check: bool = True,
    timeout: int = GIT_TIMEOUT,
    env: dict[str, str] | None = None,
) -> GitResult:
    """Run one git command with an argv list. Never uses a shell."""
    argv = [GIT, *args]
    child_env = dict(os.environ if env is None else env)
    # Keep git non-interactive: a credential prompt inside a detached worker would hang.
    child_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    child_env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git timed out after {timeout}s: {' '.join(argv)}") from exc
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    result = GitResult(argv, completed.returncode, completed.stdout, completed.stderr)
    logger.debug(
        "git %s (cwd=%s) -> exit=%d stdout=%dB stderr=%s",
        " ".join(args),
        cwd,
        result.exit_code,
        len(result.stdout),
        result.stderr.strip()[:300] or "-",
    )
    if check and not result.ok:
        raise GitError(
            f"git failed ({result.exit_code}): {' '.join(argv)}\n{result.stderr.strip()}"
        )
    return result


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def lock_path_for(locks_dir: Path, git_common_dir: Path) -> Path:
    """Return the lock file path for a repository, keyed by its common directory."""
    digest = hashlib.sha256(str(git_common_dir).encode()).hexdigest()[:32]
    locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return locks_dir / f"repo-{digest}.lock"


@contextmanager
def repo_lock(locks_dir: Path, git_common_dir: Path) -> Iterator[Path]:
    """Hold a non-blocking exclusive ``flock`` for one repository.

    The lock is advisory but process-wide and survives across MCP processes, which is
    what the "one writer per repository" invariant needs.
    """
    path = lock_path_for(locks_dir, git_common_dir)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            logger.warning("repo lock %s is already held; refusing a second writer", path)
            raise RepoLockedError(
                f"another agent_duet writer already holds {git_common_dir}; "
                "refusing to run two writers against one repository"
            ) from exc
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        logger.debug("acquired repo lock %s for %s", path, git_common_dir)
        yield path
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


# ---------------------------------------------------------------------------
# Repository inspection
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RepoInfo:
    """A validated snapshot of a repository at start time."""

    path: Path
    toplevel: Path
    git_common_dir: Path
    git_dir: Path
    branch: str | None
    detached: bool
    head_sha: str
    upstream: str | None
    remotes: dict[str, str]
    porcelain: str
    clean: bool
    submodules: list[str] = field(default_factory=list)


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=True)


def normalize_remote_url(url: str) -> str:
    """Normalize a git remote URL so equivalent spellings compare equal.

    ``git@github.com:org/repo.git`` and ``https://github.com/org/repo`` both normalize
    to ``github.com/org/repo``. Credentials embedded in the URL are stripped.
    """
    value = url.strip()
    if not value:
        return ""
    value = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", value)
    value = re.sub(r"^ssh://", "", value)
    # scp-like syntax: user@host:path
    if "://" not in url and ":" in value and not value.startswith("/"):
        value = value.replace(":", "/", 1)
    value = re.sub(r"^[^/@]+@", "", value)  # strip user@ / user:pass@
    value = re.sub(r":\d+/", "/", value, count=1)  # strip an explicit port
    value = value.removesuffix(".git")
    value = value.rstrip("/")
    return value.lower()


def _explain_broken_repo(path: Path, probe: GitResult) -> str:
    """Turn a bare `git rev-parse` failure into something an operator can act on.

    The common case in practice is a linked worktree whose parent repository was
    deleted or moved while a run was in flight. Git only says "not a git repository",
    which sends people looking in the wrong place.
    """
    dotgit = path / ".git"
    if dotgit.is_file():
        try:
            pointer = dotgit.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pointer = ""
        target = pointer.removeprefix("gitdir:").strip()
        if target and not Path(target).exists():
            return (
                f"{path} is a linked worktree whose parent repository is gone: its "
                f".git file points at {target}, which no longer exists. The canonical "
                "repository was deleted or moved while this run was in flight. Nothing "
                "was committed or pushed."
            )
    if not dotgit.exists():
        return (
            f"{path} is no longer a git repository (no .git entry). It was deleted, "
            "moved, or replaced while this run was in flight."
        )
    return f"{path} could not be inspected by git: {probe.stderr.strip() or 'unknown error'}"


def inspect_repo(repo_path: Path) -> RepoInfo:
    """Validate ``repo_path`` is a real repository and capture its exact state."""
    canonical = _canonical(repo_path)
    if not canonical.is_dir():
        raise GitError(f"{canonical} is not a directory")
    if canonical == Path("/") or canonical == Path.home():
        raise GitError(f"refusing to operate on {canonical}")
    if canonical.name == ".git":
        raise GitError("refusing to operate on a .git directory")

    toplevel_probe = run_git(["rev-parse", "--show-toplevel"], cwd=canonical, check=False)
    if not toplevel_probe.ok:
        raise GitError(_explain_broken_repo(canonical, toplevel_probe))
    toplevel = Path(toplevel_probe.stdout.strip()).resolve()
    if toplevel != canonical:
        raise GitError(
            f"{canonical} is not the repository root; git reports {toplevel}. "
            "Pass the exact repository root."
        )

    common_out = run_git(["rev-parse", "--git-common-dir"], cwd=canonical).stdout.strip()
    common = Path(common_out)
    if not common.is_absolute():
        common = (canonical / common).resolve()
    common = common.resolve()

    git_dir_out = run_git(["rev-parse", "--absolute-git-dir"], cwd=canonical).stdout.strip()

    head = run_git(["rev-parse", "HEAD"], cwd=canonical, check=False)
    if not head.ok:
        raise GitError(
            f"{canonical} has no commits yet; agent_duet requires a repository with HEAD"
        )
    head_sha = head.stdout.strip()

    symbolic = run_git(["symbolic-ref", "--quiet", "HEAD"], cwd=canonical, check=False)
    detached = not symbolic.ok
    branch_ref = symbolic.stdout.strip()
    branch = None if detached else branch_ref.removeprefix("refs/heads/")

    upstream_res = run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=canonical,
        check=False,
    )
    upstream = upstream_res.stdout.strip() if upstream_res.ok else None

    remotes: dict[str, str] = {}
    for line in run_git(["remote", "-v"], cwd=canonical).stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.setdefault(parts[0], parts[1])

    porcelain = run_git(
        ["status", "--porcelain=v2", "-z", "--untracked-files=all"], cwd=canonical
    ).stdout
    clean = porcelain.strip("\x00").strip() == ""

    submodules: list[str] = []
    if (canonical / ".gitmodules").is_file():
        sub = run_git(["submodule", "status", "--recursive"], cwd=canonical, check=False)
        submodules = [ln.strip() for ln in sub.stdout.splitlines() if ln.strip()]

    return RepoInfo(
        path=canonical,
        toplevel=toplevel,
        git_common_dir=common,
        git_dir=Path(git_dir_out),
        branch=branch,
        detached=detached,
        head_sha=head_sha,
        upstream=upstream,
        remotes=remotes,
        porcelain=porcelain,
        clean=clean,
        submodules=submodules,
    )


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Fingerprint:
    """Everything needed to prove a working tree did or did not change."""

    head_sha: str
    porcelain_sha256: str
    working_diff_sha256: str
    staged_diff_sha256: str
    remotes: dict[str, str]
    branch: str | None

    def differences(self, other: Fingerprint) -> list[str]:
        """Return human-readable descriptions of every field that differs."""
        diffs: list[str] = []
        if self.head_sha != other.head_sha:
            diffs.append(f"HEAD {self.head_sha[:12]} -> {other.head_sha[:12]}")
        if self.porcelain_sha256 != other.porcelain_sha256:
            diffs.append("working tree status changed")
        if self.working_diff_sha256 != other.working_diff_sha256:
            diffs.append("unstaged diff changed")
        if self.staged_diff_sha256 != other.staged_diff_sha256:
            diffs.append("staged diff changed")
        if self.remotes != other.remotes:
            diffs.append(f"remotes changed: {sorted(self.remotes)} -> {sorted(other.remotes)}")
        if self.branch != other.branch:
            diffs.append(f"branch {self.branch} -> {other.branch}")
        return diffs


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def fingerprint(worktree: Path) -> Fingerprint:
    """Capture the exact mutable state of ``worktree``."""
    head = run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    porcelain = run_git(
        ["status", "--porcelain=v2", "-z", "--untracked-files=all"], cwd=worktree
    ).stdout
    working = run_git(["diff", "--no-color"], cwd=worktree).stdout
    staged = run_git(["diff", "--cached", "--no-color"], cwd=worktree).stdout
    remotes: dict[str, str] = {}
    for line in run_git(["remote", "-v"], cwd=worktree).stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.setdefault(parts[0], parts[1])
    symbolic = run_git(["symbolic-ref", "--quiet", "HEAD"], cwd=worktree, check=False)
    branch_ref = symbolic.stdout.strip()
    branch = branch_ref.removeprefix("refs/heads/") if symbolic.ok else None
    return Fingerprint(
        head_sha=head,
        porcelain_sha256=_sha256_text(porcelain),
        working_diff_sha256=_sha256_text(working),
        staged_diff_sha256=_sha256_text(staged),
        remotes=remotes,
        branch=branch,
    )


def local_branch_heads(worktree: Path) -> dict[str, str]:
    """Return every local branch and its current commit.

    The active branch alone is insufficient evidence: a child can create a side branch
    and switch back before the coordinator inspects the repository.
    """
    result = run_git(
        ["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"],
        cwd=worktree,
    )
    prefix = "refs/heads/"
    return {
        full_name.removeprefix(prefix): sha
        for line in result.stdout.splitlines()
        if line.strip()
        for full_name, sha in [line.split(" ", 1)]
        if full_name.startswith(prefix)
    }


def restore_local_branch_state(
    worktree: Path,
    *,
    expected_branch: str,
    expected_heads: dict[str, str],
) -> list[str]:
    """Restore the coordinator-owned branch and report what was repaired.

    Local refs are shared by every worktree. Other branches may therefore legitimately
    move while a private review worktree is active, so this function never resets or
    deletes them. It removes an unauthorized active branch only when that ref did not
    exist before the model phase; switching this worktree to that new ref proves the ref
    belongs to the model-owned checkout. ``git switch --merge`` preserves uncommitted
    model work while returning the checkout to the selected branch.
    """
    info = inspect_repo(worktree)
    actual_heads = local_branch_heads(worktree)
    changes: list[str] = []
    if info.branch != expected_branch:
        changes.append(f"switched active branch from {info.branch!r} to {expected_branch!r}")

    expected_sha = expected_heads.get(expected_branch)
    if expected_sha is None:
        raise GitError(f"recorded branch {expected_branch!r} is missing from the branch baseline")
    if actual_heads.get(expected_branch) != expected_sha:
        changes.append(f"moved HEAD for branch {expected_branch!r}")
    if not changes:
        return []

    unauthorized_branch = info.branch
    remove_unauthorized = bool(
        unauthorized_branch
        and unauthorized_branch != expected_branch
        and unauthorized_branch not in expected_heads
    )

    # Recreate/reset the selected ref before switching in case the child deleted or
    # moved it. Updating the ref leaves the index and worktree intact.
    run_git(["update-ref", f"refs/heads/{expected_branch}", expected_sha], cwd=worktree)
    if info.branch != expected_branch:
        run_git(["switch", "--quiet", "--merge", expected_branch], cwd=worktree)

    if remove_unauthorized and unauthorized_branch:
        run_git(["update-ref", "-d", f"refs/heads/{unauthorized_branch}"], cwd=worktree)

    restored = inspect_repo(worktree)
    restored_heads = local_branch_heads(worktree)
    if restored.branch != expected_branch or restored_heads.get(expected_branch) != expected_sha:
        raise GitError("could not restore the coordinator-owned branch")
    return changes


def combined_diff_sha256(
    worktree: Path, base_sha: str, paths: Sequence[str] | None = None
) -> str:
    """Return a stable fingerprint of what changed since ``base_sha``.

    Tracked modifications come from ``git diff`` against the base commit; untracked
    files are hashed by path and content so a new file cannot slip past unnoticed.

    ``paths`` scopes the fingerprint to a known set. The coordinator passes the paths
    the *agents* changed, so artifacts a validation command produced afterwards
    (``__pycache__``, coverage data, build output) neither enter the fingerprint nor
    invalidate it if they are later cleaned up.
    """
    args = ["diff", "--no-color", base_sha]
    if paths is not None:
        args += ["--", *paths]
    tracked = run_git(args, cwd=worktree).stdout
    hasher = hashlib.sha256()
    hasher.update(tracked.encode("utf-8", errors="replace"))
    allowed = None if paths is None else set(paths)
    for rel in untracked_paths(worktree):
        if allowed is not None and rel not in allowed:
            continue
        hasher.update(b"\0untracked\0")
        hasher.update(rel.encode())
        target = worktree / rel
        try:
            hasher.update(target.read_bytes())
        except OSError:
            hasher.update(b"<unreadable>")
    return hasher.hexdigest()


def owned_tree_sha(
    worktree: Path, base_sha: str, paths: Sequence[str], index_path: Path
) -> str:
    """Return the exact git tree id that committing ``paths`` would produce.

    A textual diff digest is not content-exact: a symlink retargeted between two files
    with identical contents produces an identical patch, while the object git would
    actually commit (the link target) differs. Building a throwaway index and asking git
    to write the tree fingerprints real blob ids, file modes, symlink targets, renames,
    and deletions, so it cannot be fooled by any of those.

    ``index_path`` must live outside the repository so the run's real index is untouched.
    """
    index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    index_path.unlink(missing_ok=True)
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(index_path)
    try:
        run_git(["read-tree", base_sha], cwd=worktree, env=env)
        if paths:
            run_git(["add", "--", *paths], cwd=worktree, env=env)
        return run_git(["write-tree"], cwd=worktree, env=env).stdout.strip()
    finally:
        index_path.unlink(missing_ok=True)


def untracked_paths(worktree: Path) -> list[str]:
    """Return repo-relative paths of untracked, non-ignored files."""
    out = run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], cwd=worktree
    ).stdout
    return [part for part in out.split("\0") if part]


def changed_paths(worktree: Path, base_sha: str) -> list[str]:
    """Return every repo-relative path this run touched since ``base_sha``."""
    tracked = run_git(
        ["diff", "--name-only", "-z", base_sha], cwd=worktree
    ).stdout
    paths = {part for part in tracked.split("\0") if part}
    paths.update(untracked_paths(worktree))
    return sorted(paths)


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------


def add_worktree(repo: Path, worktree_path: Path, branch: str, base_sha: str) -> None:
    """Create ``worktree_path`` on a new ``branch`` rooted at ``base_sha``."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_git(
        ["worktree", "add", "-b", branch, str(worktree_path), base_sha],
        cwd=repo,
    )
    worktree_path.chmod(0o700)


def remove_worktree(repo: Path, worktree_path: Path, *, force: bool = False) -> GitResult:
    """Remove a worktree registration. Never deletes the repository itself."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    return run_git(args, cwd=repo, check=False)


def prune_worktrees(repo: Path) -> GitResult:
    """Drop stale worktree registrations."""
    return run_git(["worktree", "prune"], cwd=repo, check=False)


# ---------------------------------------------------------------------------
# Finalization primitives
# ---------------------------------------------------------------------------


def stage_paths(worktree: Path, paths: Sequence[str]) -> None:
    """Stage exactly ``paths`` and nothing else. ``git add -A`` is never used."""
    if not paths:
        raise GitError("refusing to commit: no run-owned paths to stage")
    # `--` terminates option parsing so a path starting with '-' cannot become a flag.
    run_git(["add", "--", *paths], cwd=worktree)


def staged_paths(worktree: Path) -> list[str]:
    """Return the exact staged path list."""
    out = run_git(["diff", "--cached", "--name-only", "-z"], cwd=worktree).stdout
    return sorted(part for part in out.split("\0") if part)


def write_tree(worktree: Path) -> str:
    """Return the tree object id of the current index."""
    return run_git(["write-tree"], cwd=worktree).stdout.strip()


def commit(worktree: Path, message: str) -> str:
    """Create a commit from the current index and return its exact SHA."""
    # The message is passed on stdin so it never appears in argv or process listings.
    completed = subprocess.run(
        [GIT, "commit", "--no-verify", "--file", "-"],
        cwd=str(worktree),
        input=message,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise GitError(f"git commit failed: {completed.stderr.strip()}")
    return run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()


def push_branch(worktree: Path, remote: str, branch: str) -> GitResult:
    """Push exactly one branch, never with a force flag."""
    return run_git(
        ["push", "--set-upstream", remote, f"refs/heads/{branch}:refs/heads/{branch}"],
        cwd=worktree,
        check=True,
        timeout=900,
    )


def remote_sha(worktree: Path, remote: str, branch: str) -> str | None:
    """Resolve the remote ref to an exact SHA by asking the remote, not the cache."""
    result = run_git(
        ["ls-remote", "--exit-code", remote, f"refs/heads/{branch}"],
        cwd=worktree,
        check=False,
        timeout=300,
    )
    if not result.ok:
        return None
    first = result.stdout.split()
    return first[0] if first else None
