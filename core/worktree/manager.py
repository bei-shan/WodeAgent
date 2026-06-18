"""WorktreeManager — git worktree lifecycle management.

Aligns with Claude Code's EnterWorktree / ExitWorktree model.
All git operations run via subprocess with timeout protection.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

from .store import WorktreeStore

logger = logging.getLogger(__name__)


class WorktreeError(Exception):
    """Typed error with code for tool-level ErrorCode mapping."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class WorktreeManager:
    """Manages git worktree lifecycle for session-level isolation.

    Directory layout::

        .worktrees/
        ├── index.json
        ├── try-oauth/        # git worktree
        └── auth-refactor/    # git worktree

    Branch naming: ``wt/{name}``

    Parameters
    ----------
    project_root:
        The git repository root.
    store_dir:
        Directory for worktree files (default: WORKTREE_STORE_DIR or ".worktrees").
    base_ref:
        "fresh" — use origin/HEAD (safe default, avoids dirty state).
        "head"  — use current HEAD.
    """

    _NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

    def __init__(
        self,
        project_root: Path,
        store_dir: str | None = None,
        base_ref: str = "fresh",
    ):
        self._root = Path(project_root).resolve()
        dir_name = store_dir or os.getenv("WORKTREE_STORE_DIR", ".worktrees")
        self._worktrees_dir = self._root / dir_name
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._store = WorktreeStore(self._worktrees_dir)
        self._base_ref_mode = str(base_ref or "fresh").strip().lower()
        self._git_available: bool | None = None  # lazy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, name: str) -> dict:
        """Create a new git worktree.

        1. Validate *name*.
        2. Reject if a worktree with the same name is already active/kept.
        3. Resolve ``base_ref``.
        4. ``git worktree add -b wt/{name} .worktrees/{name} {base_ref}``
        5. Persist entry to index.json.

        Returns the worktree entry dict.

        Raises
        ------
        WorktreeError(INVALID_PARAM)
            If *name* is invalid.
        WorktreeError(CONFLICT)
            If a worktree with *name* already exists.
        WorktreeError(INTERNAL_ERROR)
            If git is unavailable or the command fails.
        """
        name = self._validate_name(name)
        self._ensure_git_available()

        # Check for duplicate
        existing = self._store.find(name)
        if existing and existing.get("status") in ("active", "kept"):
            raise WorktreeError(
                "CONFLICT",
                f"worktree '{name}' already exists. "
                "Use EnterWorktree(path=...) to enter an existing worktree.",
            )

        base_ref = self._resolve_base_ref()
        branch = f"wt/{name}"
        wt_path = self._worktrees_dir / name

        logger.info("Creating worktree '%s': branch=%s, base=%s", name, branch, base_ref)
        self._run_git(["worktree", "add", "-b", branch, str(wt_path), base_ref])

        entry = {
            "name": name,
            "path": str(wt_path.resolve()),
            "branch": branch,
            "base_ref": base_ref,
            "status": "active",
            "created_at": time.time(),
            "removed_at": None,
            "kept_at": None,
        }
        self._store.save(entry)
        logger.info("Worktree '%s' created at %s", name, entry["path"])
        return dict(entry)

    def get_by_path(self, path: str) -> dict:
        """Find a worktree by its filesystem path.

        Used by ``EnterWorktree(path=...)`` to re-enter an existing worktree.
        The path must be registered in index.json.

        Raises WorktreeError(NOT_FOUND) if no entry matches.
        """
        resolved = str(Path(path).resolve())
        for entry in self._store.load_all():
            if entry.get("path") == resolved:
                return dict(entry)
        raise WorktreeError("NOT_FOUND", f"no worktree registered at: {path}")

    def remove(self, name: str, *, discard_changes: bool = False) -> dict:
        """Remove a worktree and its branch.

        - If *discard_changes* is False and the worktree has uncommitted
          changes → WorktreeError(CONFLICT).
        - If *discard_changes* is True → ``git worktree remove --force``.
        - The branch ``wt/{name}`` is also deleted (best-effort).

        Returns the updated entry dict with status="removed".

        Raises
        ------
        WorktreeError(NOT_FOUND)
            If *name* is not registered.
        WorktreeError(CONFLICT)
            If uncommitted changes exist and *discard_changes* is False.
        """
        name = self._validate_name(name)
        self._ensure_git_available()
        entry = self._store.find(name)
        if entry is None:
            raise WorktreeError("NOT_FOUND", f"worktree not found: {name}")

        wt_path = Path(entry["path"])
        if not discard_changes and not self.is_clean(name):
            raise WorktreeError(
                "CONFLICT",
                f"worktree '{name}' has uncommitted changes. "
                "Use discard_changes=true to force remove.",
            )

        logger.info("Removing worktree '%s' (discard_changes=%s)", name, discard_changes)
        args = ["worktree", "remove"]
        if discard_changes:
            args.append("--force")
        args.append(str(wt_path))
        self._run_git(args)

        # Remove the branch (best-effort — may already be gone)
        try:
            self._run_git(["branch", "-D", entry["branch"]])
        except WorktreeError:
            pass

        entry["status"] = "removed"
        entry["removed_at"] = time.time()
        self._store.save(entry)
        logger.info("Worktree '%s' removed", name)
        return dict(entry)

    def keep(self, name: str) -> dict:
        """Mark a worktree as kept.

        No git command is executed — only the index entry is updated.
        The caller is responsible for merging and final cleanup.

        Returns the updated entry dict with status="kept".
        """
        name = self._validate_name(name)
        entry = self._store.find(name)
        if entry is None:
            raise WorktreeError("NOT_FOUND", f"worktree not found: {name}")

        entry["status"] = "kept"
        entry["kept_at"] = time.time()
        self._store.save(entry)
        logger.info("Worktree '%s' kept (branch: %s)", name, entry.get("branch"))
        return dict(entry)

    def is_clean(self, name: str) -> bool:
        """Return True if the worktree has no uncommitted changes."""
        entry = self._store.find(name)
        if entry is None:
            raise WorktreeError("NOT_FOUND", f"worktree not found: {name}")
        try:
            result = self._run_git(
                ["status", "--porcelain"],
                cwd=Path(entry["path"]),
            )
            return result.strip() == ""
        except WorktreeError:
            return False

    def has_changes(self, name: str) -> bool:
        """Return True if the worktree has any changes (including untracked)."""
        return not self.is_clean(name)

    def list_all(self) -> list[dict]:
        """Return all worktree entries, sorted by created_at."""
        return self._store.load_all()

    def list_git_worktrees(self) -> list[str]:
        """Run ``git worktree list`` and return output lines.

        Returns an empty list if git is unavailable.
        """
        try:
            self._ensure_git_available()
            output = self._run_git(["worktree", "list"])
            return [line for line in output.strip().splitlines() if line.strip()]
        except WorktreeError:
            return []

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_git_available(self) -> None:
        """Check that git is available and we are in a repo.

        Raises WorktreeError(INTERNAL_ERROR) if not.
        """
        if self._git_available is True:
            return
        try:
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            self._git_available = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            self._git_available = False
            raise WorktreeError(
                "INTERNAL_ERROR",
                "git is not available or not a git repository. "
                "Worktree operations require a git repository.",
            )
        except subprocess.TimeoutExpired:
            self._git_available = False
            raise WorktreeError("TIMEOUT", "git check timed out")

    def _resolve_base_ref(self) -> str:
        """Compute the base reference for new worktrees.

        - "fresh": use ``origin/HEAD`` → ``refs/remotes/origin/main``.
          Falls back to ``HEAD`` if origin/HEAD is unavailable.
        - "head": use ``HEAD`` (current branch tip).
        """
        if self._base_ref_mode == "fresh":
            try:
                ref = self._run_git(["rev-parse", "--abbrev-ref", "origin/HEAD"]).strip()
                if ref:
                    return ref
            except WorktreeError:
                pass
        return "HEAD"

    def _validate_name(self, name: str) -> str:
        """Validate and sanitize a worktree name.

        Rules: 1-64 chars, letters/digits/dots/underscores/dashes only.
        Rejects path traversal characters.
        """
        if not isinstance(name, str) or not name.strip():
            raise WorktreeError("INVALID_PARAM", "name is required")
        name = name.strip()
        if len(name) > 64:
            raise WorktreeError("INVALID_PARAM", "name must be <= 64 characters")
        for char in ("/", "\\", "..", " ", ":", "*", "?", '"', "<", ">", "|"):
            if char in name:
                raise WorktreeError(
                    "INVALID_PARAM",
                    f"name contains invalid character: '{char}'",
                )
        if not self._NAME_RE.match(name):
            raise WorktreeError(
                "INVALID_PARAM",
                "name may only contain letters, digits, dots, underscores, and dashes",
            )
        return name

    def _run_git(
        self,
        args: list[str],
        cwd: Path | None = None,
        timeout_s: float = 30.0,
    ) -> str:
        """Run a git command, return stdout, raise WorktreeError on failure.

        Parameters
        ----------
        args:
            Git subcommand arguments (without "git").
        cwd:
            Working directory for the command. Defaults to repo root.
        timeout_s:
            Timeout in seconds.
        """
        cmd = ["git"] + args
        work_dir = str(cwd or self._root)
        try:
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                raise WorktreeError(
                    "INTERNAL_ERROR",
                    f"git {' '.join(args)} failed: {stderr}",
                )
            return result.stdout
        except subprocess.TimeoutExpired:
            raise WorktreeError(
                "TIMEOUT",
                f"git {' '.join(args)} timed out after {timeout_s}s",
            )
