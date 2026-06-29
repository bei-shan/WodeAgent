"""WorktreeFeature init/post_init tests — non-git pre-check.

Run:
    python -m pytest tests/test_worktree_feature.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.features.worktree import WorktreeFeature
from tools.base import Tool, ToolParameter
from tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubWorktreeTool(Tool):
    """Bare-minimum stand-in for EnterWorktreeTool / ExitWorktreeTool — just
    enough that we can register, query, and unregister it."""

    def __init__(self, name: str, project_root: Path):
        super().__init__(
            name=name,
            description=f"{name} (stub)",
            project_root=str(project_root),
            working_dir=str(project_root),
        )

    def get_parameters(self) -> list[ToolParameter]:
        return []

    def run(self, parameters):  # pragma: no cover - not exercised
        return "{}"


def _make_agent(project_root: Path) -> SimpleNamespace:
    """Build a minimal agent shaped to satisfy WorktreeFeature.init/post_init."""
    registry = ToolRegistry()
    # Pre-register the two worktree tools to mimic the order: ToolBootstrap
    # populates the registry between feature.init() and feature.post_init().
    registry.register_tool(_StubWorktreeTool("EnterWorktree", project_root))
    registry.register_tool(_StubWorktreeTool("ExitWorktree", project_root))
    return SimpleNamespace(
        config=SimpleNamespace(worktree_store_dir=".worktrees", worktree_base_ref="fresh"),
        _original_project_root=str(project_root),
        tool_registry=registry,
    )


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo at ``path``. Skips the test if git is missing."""
    try:
        subprocess.run(
            ["git", "init"],
            cwd=str(path),
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("git not available on this test runner")


# ---------------------------------------------------------------------------
# Non-git directory: worktree tools should be unregistered, flag set
# ---------------------------------------------------------------------------


def test_worktree_feature_disables_on_non_git_dir(tmp_path):
    """Non-git project root → _worktree_disabled=True and both worktree
    tools are removed from the registry by post_init()."""
    agent = _make_agent(tmp_path)
    feat = WorktreeFeature()

    feat.init(agent)
    # init() must set the flag and leave the tools registered (post_init removes).
    assert agent._worktree_disabled is True
    assert agent._worktree_disabled_reason  # non-empty reason
    assert agent.tool_registry.get_tool("EnterWorktree") is not None
    assert agent.tool_registry.get_tool("ExitWorktree") is not None

    feat.post_init(agent)
    assert agent.tool_registry.get_tool("EnterWorktree") is None
    assert agent.tool_registry.get_tool("ExitWorktree") is None


# ---------------------------------------------------------------------------
# Real git directory: tools stay, flag cleared
# ---------------------------------------------------------------------------


def test_worktree_feature_keeps_tools_in_git_dir(tmp_path):
    """git-initialized project root → _worktree_disabled=False and both
    tools remain registered."""
    _init_git_repo(tmp_path)
    agent = _make_agent(tmp_path)
    feat = WorktreeFeature()

    feat.init(agent)
    assert agent._worktree_disabled is False
    assert not hasattr(agent, "_worktree_disabled_reason") or not agent._worktree_disabled_reason

    feat.post_init(agent)
    assert agent.tool_registry.get_tool("EnterWorktree") is not None
    assert agent.tool_registry.get_tool("ExitWorktree") is not None


# ---------------------------------------------------------------------------
# Idempotency: post_init() runs even if tools were never registered
# ---------------------------------------------------------------------------


def test_worktree_feature_post_init_idempotent_when_tools_missing(tmp_path):
    """If post_init runs when worktree tools were never registered (e.g.
    in tests or stripped-down environments) it should not raise."""
    agent = _make_agent(tmp_path)
    # Strip the tools that _make_agent pre-registered.
    agent.tool_registry.unregister("EnterWorktree")
    agent.tool_registry.unregister("ExitWorktree")

    feat = WorktreeFeature()
    feat.init(agent)
    feat.post_init(agent)  # must not raise
