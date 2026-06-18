"""EnterWorktreeTool / ExitWorktreeTool tests.

Run:
    python -m pytest tests/test_worktree_tools.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

import pytest

from core.worktree.manager import WorktreeError, WorktreeManager
from tools.builtin.enter_worktree import EnterWorktreeTool
from tools.builtin.exit_worktree import ExitWorktreeTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(response: str) -> dict:
    return json.loads(response)


def _mock_worktree_manager(manager_data: dict | None = None):
    """Create a mock WorktreeManager with canned responses."""
    mgr = Mock(spec=WorktreeManager)
    if manager_data:
        mgr.create.return_value = manager_data
        mgr.get_by_path.return_value = manager_data
    return mgr


def _mock_code_agent(active_worktree: dict | None = None):
    """Create a mock CodeAgent."""
    agent = Mock()
    agent._active_worktree = active_worktree
    agent.project_root = "/original/project"
    agent._original_project_root = "/original/project"
    agent.enter_worktree = Mock()
    agent.exit_worktree = Mock()
    return agent


# ---------------------------------------------------------------------------
# EnterWorktreeTool
# ---------------------------------------------------------------------------

class TestEnterWorktreeTool:
    def test_create_new_worktree(self, tmp_path: Path):
        wt_data = {
            "name": "feat-oauth",
            "path": "/tmp/.worktrees/feat-oauth",
            "branch": "wt/feat-oauth",
            "base_ref": "HEAD",
            "status": "active",
            "created_at": 1730000000.0,
        }
        mgr = _mock_worktree_manager(wt_data)
        agent = _mock_code_agent()
        tool = EnterWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"name": "feat-oauth"}))
        assert response["status"] == "success"
        assert response["data"]["name"] == "feat-oauth"
        assert response["data"]["branch"] == "wt/feat-oauth"
        # Verify manager.create was called
        mgr.create.assert_called_once_with(name="feat-oauth")
        # Verify agent.enter_worktree was called
        agent.enter_worktree.assert_called_once()

    def test_enter_existing_by_path(self, tmp_path: Path):
        wt_data = {
            "name": "existing-wt",
            "path": "/home/user/.worktrees/existing",
            "branch": "wt/existing-wt",
            "status": "active",
            "created_at": 1730000000.0,
        }
        mgr = _mock_worktree_manager(wt_data)
        agent = _mock_code_agent()
        tool = EnterWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"path": "/home/user/.worktrees/existing"}))
        assert response["status"] == "success"
        mgr.get_by_path.assert_called_once_with(path="/home/user/.worktrees/existing")
        agent.enter_worktree.assert_called_once()

    def test_missing_both_name_and_path(self, tmp_path: Path):
        mgr = _mock_worktree_manager()
        agent = _mock_code_agent()
        tool = EnterWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({}))
        assert response["status"] == "error"
        assert "exactly one" in response["text"].lower()

    def test_both_name_and_path_provided(self, tmp_path: Path):
        mgr = _mock_worktree_manager()
        agent = _mock_code_agent()
        tool = EnterWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"name": "x", "path": "/x"}))
        assert response["status"] == "error"
        assert "exactly one" in response["text"].lower()

    def test_empty_name_rejected(self, tmp_path: Path):
        mgr = _mock_worktree_manager()
        mgr.create.side_effect = WorktreeError("INVALID_PARAM", "name is required")
        agent = _mock_code_agent()
        tool = EnterWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"name": ""}))
        assert response["status"] == "error"

    def test_create_conflict_error(self, tmp_path: Path):
        mgr = _mock_worktree_manager()
        mgr.create.side_effect = WorktreeError("CONFLICT", "already exists")
        agent = _mock_code_agent()
        tool = EnterWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"name": "dup"}))
        assert response["status"] == "error"
        assert "already exists" in response["text"]

    def test_get_by_path_not_found(self, tmp_path: Path):
        mgr = _mock_worktree_manager()
        mgr.get_by_path.side_effect = WorktreeError("NOT_FOUND", "no worktree")
        agent = _mock_code_agent()
        tool = EnterWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"path": "/nonexistent"}))
        assert response["status"] == "error"

    def test_response_includes_params_input(self, tmp_path: Path):
        wt_data = {"name": "x", "path": "/x", "branch": "wt/x", "status": "active", "created_at": 1}
        mgr = _mock_worktree_manager(wt_data)
        agent = _mock_code_agent()
        tool = EnterWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"name": "x"}))
        # params_input should contain original parameters (under context)
        context = response.get("context", {})
        assert "params_input" in context
        assert context["params_input"] == {"name": "x"}


# ---------------------------------------------------------------------------
# ExitWorktreeTool
# ---------------------------------------------------------------------------

class TestExitWorktreeTool:
    def test_exit_keep_success(self, tmp_path: Path):
        active_wt = {
            "name": "my-wt",
            "path": "/tmp/.worktrees/my-wt",
            "branch": "wt/my-wt",
        }
        mgr = _mock_worktree_manager()
        mgr.is_clean.return_value = False
        mgr.keep.return_value = {"name": "my-wt", "status": "kept", "kept_at": 1}
        agent = _mock_code_agent(active_worktree=active_wt)
        tool = ExitWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"action": "keep"}))
        assert response["status"] == "success"
        assert "merge" in response["text"].lower() or "git merge" in response["text"].lower()
        mgr.keep.assert_called_once_with("my-wt")
        agent.exit_worktree.assert_called_once()

    def test_exit_remove_success(self, tmp_path: Path):
        active_wt = {"name": "to-del", "path": "/tmp/.worktrees/to-del"}
        mgr = _mock_worktree_manager()
        mgr.is_clean.return_value = True  # clean → auto-remove
        mgr.remove.return_value = {"name": "to-del", "status": "removed", "removed_at": 1}
        agent = _mock_code_agent(active_worktree=active_wt)
        tool = ExitWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"action": "remove"}))
        assert response["status"] == "success"
        agent.exit_worktree.assert_called_once()

    def test_exit_remove_discard_changes(self, tmp_path: Path):
        active_wt = {"name": "dirty", "path": "/tmp/.worktrees/dirty"}
        mgr = _mock_worktree_manager()
        mgr.is_clean.return_value = False
        mgr.remove.return_value = {"name": "dirty", "status": "removed", "removed_at": 1}
        agent = _mock_code_agent(active_worktree=active_wt)
        tool = ExitWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"action": "remove", "discard_changes": True}))
        assert response["status"] == "success"
        mgr.remove.assert_called_once_with("dirty", discard_changes=True)

    def test_exit_not_in_worktree(self, tmp_path: Path):
        mgr = _mock_worktree_manager()
        agent = _mock_code_agent(active_worktree=None)
        tool = ExitWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"action": "keep"}))
        assert response["status"] == "error"
        assert "not currently in a worktree" in response["text"].lower()

    def test_exit_remove_dirty_without_discard_rejected(self, tmp_path: Path):
        active_wt = {"name": "dirty", "path": "/tmp/.worktrees/dirty"}
        mgr = _mock_worktree_manager()
        mgr.is_clean.return_value = False
        agent = _mock_code_agent(active_worktree=active_wt)
        tool = ExitWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"action": "remove", "discard_changes": False}))
        assert response["status"] == "error"
        assert "uncommitted" in response["text"].lower() or "changes" in response["text"].lower()

    def test_exit_auto_removes_clean_worktree(self, tmp_path: Path):
        """Clean worktrees should be auto-removed regardless of action choice."""
        active_wt = {"name": "clean-wt", "path": "/tmp/.worktrees/clean-wt"}
        mgr = _mock_worktree_manager()
        mgr.is_clean.return_value = True
        mgr.remove.return_value = {"name": "clean-wt", "status": "removed", "removed_at": 1}
        agent = _mock_code_agent(active_worktree=active_wt)
        tool = ExitWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        # Even with action="keep", clean worktree should auto-remove
        response = _parse_json(tool.run({"action": "keep"}))
        assert response["status"] == "success"
        mgr.remove.assert_called_once()  # remove called, not keep
        # Text should mention auto-removal
        assert "no changes" in response["text"].lower() or "auto" in response["text"].lower()

    def test_invalid_action_rejected(self, tmp_path: Path):
        active_wt = {"name": "x", "path": "/x"}
        mgr = _mock_worktree_manager()
        agent = _mock_code_agent(active_worktree=active_wt)
        tool = ExitWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"action": "invalid"}))
        assert response["status"] == "error"

    def test_exit_tool_always_restores_root_on_error(self, tmp_path: Path):
        """Even if git cleanup fails, project_root should be restored."""
        active_wt = {"name": "bad", "path": "/tmp/.worktrees/bad"}
        mgr = _mock_worktree_manager()
        mgr.is_clean.return_value = False
        mgr.keep.side_effect = WorktreeError("INTERNAL_ERROR", "git failed")
        agent = _mock_code_agent(active_worktree=active_wt)
        tool = ExitWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"action": "keep"}))
        # Should return error from git failure
        assert response["status"] == "error"
        # But project_root should still have been restored
        agent.exit_worktree.assert_called_once()

    def test_params_input_present(self, tmp_path: Path):
        active_wt = {"name": "x", "path": "/x"}
        mgr = _mock_worktree_manager()
        mgr.is_clean.return_value = True
        mgr.remove.return_value = {"name": "x", "status": "removed", "removed_at": 1}
        agent = _mock_code_agent(active_worktree=active_wt)
        tool = ExitWorktreeTool(
            project_root=tmp_path,
            worktree_manager=mgr,
            code_agent=agent,
        )
        response = _parse_json(tool.run({"action": "remove"}))
        context = response.get("context", {})
        assert "params_input" in context
