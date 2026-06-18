"""Soft sandbox permission gate for file system access.

Replaces hard sandbox (always deny outside project root) with a user-confirmation
model: when a tool tries to access a path outside the project root, the user is
prompted via ``input()`` to grant or deny access.  Granted paths are cached
for the duration of the agent session.

Behaviour is controlled by:
- ``PERMISSION_SOFT_SANDBOX`` env var (default ``true``).  Set to ``false`` to
  restore hard-sandbox behaviour (always deny outside project root).
- ``AGENT_INTERACTIVE`` env var (default ``true``).  When ``false``, the
  ``ask()`` method returns ``"denied"`` without blocking.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Path patterns that are *always* denied, even in soft-sandbox mode.
_ALWAYS_DENY_PATTERNS: list[str] = [
    # Unix
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/ssh/",
    "/root/",
    "/proc/",
    "/sys/",
    # macOS
    "/System/",
    "/Library/Keychains/",
    # Windows
    "C:\\Windows\\System32\\",
    "C:\\Windows\\System\\",
    # Sensitive dotfiles
    ".ssh/id_rsa",
    ".ssh/id_ed25519",
    ".aws/credentials",
    ".env.production",
]


def _is_always_denied(resolved_path: str) -> bool:
    """Check whether *resolved_path* matches any permanent-deny pattern."""
    lower = resolved_path.lower().replace("\\", "/")
    for pattern in _ALWAYS_DENY_PATTERNS:
        if pattern.lower().replace("\\", "/") in lower:
            return True
    return False


class PermissionGate:
    """Manages path-access permissions for a single agent session.

    Parameters
    ----------
    project_root:
        The agent's workspace root.  Paths inside this directory are always
        allowed without asking.
    interactive:
        When ``False``, ``ask()`` returns ``"denied"`` immediately.  Used for
        sub-agents that should not block the UI thread.
    soft_sandbox:
        When ``False``, all out-of-root paths are denied without asking
        (hard-sandbox fallback).
    cache_size:
        Maximum number of cached decisions.  When exceeded the oldest entry
        is evicted.
    """

    __slots__ = (
        "_project_root",
        "_interactive",
        "_soft_sandbox",
        "_cache",
        "_cache_size",
    )

    def __init__(
        self,
        project_root: Path,
        interactive: bool = True,
        soft_sandbox: bool = True,
        cache_size: int = 500,
    ):
        self._project_root = Path(project_root).resolve()
        self._interactive = bool(interactive)
        self._soft_sandbox = bool(soft_sandbox)
        self._cache: dict[str, str] = {}  # resolved_path → "granted" | "denied"
        self._cache_size = max(1, int(cache_size))

    def subagent_gate(self) -> "PermissionGate":
        """Return a non-interactive view that shares the same cache.

        Sub-agents use this to inherit the main agent's authorization cache
        without being able to prompt the user directly.
        """
        gate = PermissionGate(
            project_root=self._project_root,
            interactive=False,
            soft_sandbox=self._soft_sandbox,
            cache_size=self._cache_size,
        )
        gate._cache = self._cache  # Share cache reference
        return gate

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_within_root(self, resolved_path: str) -> bool:
        """Return True if *resolved_path* is inside the project root."""
        try:
            Path(resolved_path).resolve().relative_to(self._project_root)
            return True
        except ValueError:
            return False

    def check(self, resolved_path: str) -> str:
        """Return the access decision for *resolved_path*.

        Returns one of:
        - ``"granted"`` — allowed (inside root or previously approved)
        - ``"denied"``  — permanently denied or previously refused
        - ``"ask"``     — needs user confirmation
        """
        # Always deny sensitive paths.
        if _is_always_denied(resolved_path):
            return "denied"

        # Inside project root → always granted.
        if self.is_within_root(resolved_path):
            return "granted"

        # Hard-sandbox fallback.
        if not self._soft_sandbox:
            return "denied"

        # Check session cache.
        cached = self._cache.get(resolved_path)
        if cached is not None:
            return cached

        # Needs user confirmation.
        if self._interactive:
            return "ask"

        # Non-interactive → deny.
        return "denied"

    def ask(self, resolved_path: str, tool_name: str, action: str) -> str:
        """Prompt the user and cache the decision.

        Returns ``"granted"`` or ``"denied"``.  Does NOT block when
        ``interactive=False`` (returns ``"denied"`` immediately).
        """
        if not self._interactive:
            self._cache_decision(resolved_path, "denied")
            return "denied"

        # Build prompt.
        rel = self._try_relpath(resolved_path)
        display = rel if rel else resolved_path
        prompt = (
            f"\n{'═' * 60}\n"
            f"  🔒 权限请求\n"
            f"  {tool_name} 工具尝试访问项目外的{'目录' if action == 'cd' else '文件'}:\n"
            f"  {display}\n"
            f"  操作: {action}\n"
            f"  允许访问? [y/N] "
        )

        try:
            answer = input(prompt)
        except EOFError:
            answer = ""

        decision = "granted" if answer.strip().lower() in ("y", "yes") else "denied"
        self._cache_decision(resolved_path, decision)

        if decision == "granted":
            logger.info("User granted access to %s", resolved_path)
        else:
            logger.info("User denied access to %s", resolved_path)

        return decision

    def grant(self, resolved_path: str) -> None:
        """Pre-authorise *resolved_path* (no prompt)."""
        self._cache_decision(resolved_path, "granted")

    def clear_cache(self) -> None:
        """Clear all cached decisions."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cache_decision(self, resolved_path: str, decision: str) -> None:
        if len(self._cache) >= self._cache_size:
            # Evict oldest entry (simple FIFO).
            first_key = next(iter(self._cache))
            del self._cache[first_key]
        self._cache[resolved_path] = decision

    def _try_relpath(self, resolved_path: str) -> Optional[str]:
        """Return a relative path if inside project root, else None."""
        try:
            return Path(resolved_path).relative_to(self._project_root).as_posix()
        except ValueError:
            return None


def create_permission_gate(
    project_root: str,
    interactive: bool | None = None,
) -> PermissionGate:
    """Factory: create a PermissionGate from env vars.

    Reads:
    - ``PERMISSION_SOFT_SANDBOX`` (default ``true``)
    - ``AGENT_INTERACTIVE`` (default ``true``)
    - ``PERMISSION_CACHE_SIZE`` (default ``500``)
    """
    soft = os.environ.get("PERMISSION_SOFT_SANDBOX", "true").lower() in (
        "1", "true", "yes", "y", "on",
    )
    if interactive is None:
        interactive = os.environ.get("AGENT_INTERACTIVE", "true").lower() in (
            "1", "true", "yes", "y", "on",
        )
    cache_size = int(os.environ.get("PERMISSION_CACHE_SIZE", "500"))
    return PermissionGate(
        project_root=Path(project_root),
        interactive=interactive,
        soft_sandbox=soft,
        cache_size=cache_size,
    )
