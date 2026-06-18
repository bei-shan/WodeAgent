"""WorktreeManager unit tests (git mocked).

Run:
    python -m pytest tests/test_worktree_manager.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.worktree.manager import WorktreeError, WorktreeManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wt_manager(tmp_path: Path):
    """WorktreeManager with git mocked to always succeed."""
    mgr = WorktreeManager(project_root=tmp_path, base_ref="head")
    # Mock _run_git to return empty string (success)
    mgr._run_git = Mock(return_value="")
    # Mock _ensure_git_available to no-op
    mgr._ensure_git_available = Mock()
    return mgr


def _make_git_status_mock(output: str):
    """Helper: create a _run_git mock that returns cleaned output for
    'git status --porcelain' but empty string for other commands.
    """
    def _side_effect(args, cwd=None, timeout_s=30.0):
        if "status" in args and "--porcelain" in args:
            return output
        return ""
    return Mock(side_effect=_side_effect)


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

class TestNameValidation:
    def test_valid_names(self, wt_manager):
        for name in ["fix-bug", "feat.login_api", "refactor_v2", "x", "a" * 64]:
            result = wt_manager._validate_name(name)
            assert result == name

    def test_rejects_empty(self, wt_manager):
        with pytest.raises(WorktreeError, match="name is required"):
            wt_manager._validate_name("")
        with pytest.raises(WorktreeError, match="name is required"):
            wt_manager._validate_name("   ")

    def test_rejects_too_long(self, wt_manager):
        with pytest.raises(WorktreeError, match="<= 64"):
            wt_manager._validate_name("a" * 65)

    @pytest.mark.parametrize("bad_char", ["/", "\\", "..", " ", ":", "*", "?", '"', "<", ">", "|"])
    def test_rejects_path_traversal(self, wt_manager, bad_char):
        with pytest.raises(WorktreeError, match="invalid character"):
            wt_manager._validate_name(f"hello{bad_char}world")

    def test_rejects_non_ascii(self, wt_manager):
        with pytest.raises(WorktreeError, match="only contain"):
            wt_manager._validate_name("hello@world")

    def test_rejects_none(self, wt_manager):
        with pytest.raises(WorktreeError, match="name is required"):
            wt_manager._validate_name(None)

    def test_not_a_number(self, wt_manager):
        with pytest.raises(WorktreeError, match="name is required"):
            wt_manager._validate_name(123)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_success(self, wt_manager):
        entry = wt_manager.create("test-wt")
        assert entry["name"] == "test-wt"
        assert entry["branch"] == "wt/test-wt"
        assert entry["base_ref"] == "HEAD"
        assert entry["status"] == "active"
        assert "created_at" in entry
        # Verify git command was called
        wt_manager._run_git.assert_called_once()
        args = wt_manager._run_git.call_args[0][0]
        assert args[0] == "worktree"
        assert args[1] == "add"

    def test_create_saves_to_index(self, wt_manager):
        wt_manager.create("wt1")
        entries = wt_manager.list_all()
        assert len(entries) == 1
        assert entries[0]["name"] == "wt1"

    def test_create_duplicate_rejected(self, wt_manager):
        wt_manager.create("dup")
        with pytest.raises(WorktreeError, match="already exists"):
            wt_manager.create("dup")

    def test_create_after_removed_allowed(self, wt_manager):
        wt_manager.create("reused")
        entry = wt_manager._store.find("reused")
        entry["status"] = "removed"
        wt_manager._store.save(entry)
        # Creating again should work (removed is not active/kept)
        entry = wt_manager.create("reused")
        assert entry["status"] == "active"

    def test_create_after_kept_rejected(self, wt_manager):
        wt_manager.create("kept-wt")
        entry = wt_manager._store.find("kept-wt")
        entry["status"] = "kept"
        wt_manager._store.save(entry)
        with pytest.raises(WorktreeError, match="already exists"):
            wt_manager.create("kept-wt")


# ---------------------------------------------------------------------------
# Get by path
# ---------------------------------------------------------------------------

class TestGetByPath:
    def test_find_by_path(self, wt_manager):
        wt_manager.create("path-test")
        entry = wt_manager.list_all()[0]
        found = wt_manager.get_by_path(entry["path"])
        assert found["name"] == "path-test"

    def test_not_found_by_path(self, wt_manager):
        with pytest.raises(WorktreeError, match="no worktree registered"):
            wt_manager.get_by_path("/nonexistent/path")


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------

class TestRemove:
    def test_remove_success(self, wt_manager):
        wt_manager.create("to-remove")
        # Mock is_clean to return True
        wt_manager.is_clean = Mock(return_value=True)
        entry = wt_manager.remove("to-remove")
        assert entry["status"] == "removed"
        assert "removed_at" in entry
        # Verify git worktree remove was called
        assert any("remove" in str(call) for call in wt_manager._run_git.call_args_list)

    def test_remove_not_found(self, wt_manager):
        with pytest.raises(WorktreeError, match="not found"):
            wt_manager.remove("nonexistent")

    def test_remove_with_changes_rejected(self, wt_manager):
        wt_manager.create("dirty")
        wt_manager.is_clean = Mock(return_value=False)
        with pytest.raises(WorktreeError, match="uncommitted changes"):
            wt_manager.remove("dirty")

    def test_remove_discard_changes_allowed(self, wt_manager):
        wt_manager.create("dirty")
        wt_manager.is_clean = Mock(return_value=False)
        entry = wt_manager.remove("dirty", discard_changes=True)
        assert entry["status"] == "removed"

    def test_remove_clean_requires_no_discard(self, wt_manager):
        wt_manager.create("clean")
        wt_manager.is_clean = Mock(return_value=True)
        entry = wt_manager.remove("clean")
        assert entry["status"] == "removed"


# ---------------------------------------------------------------------------
# Keep
# ---------------------------------------------------------------------------

class TestKeep:
    def test_keep_success(self, wt_manager):
        wt_manager.create("to-keep")
        entry = wt_manager.keep("to-keep")
        assert entry["status"] == "kept"
        assert "kept_at" in entry

    def test_keep_not_found(self, wt_manager):
        with pytest.raises(WorktreeError, match="not found"):
            wt_manager.keep("nonexistent")

    def test_keep_does_not_call_git(self, wt_manager):
        wt_manager.create("no-git-call")
        # Reset mock count after create
        call_count_before = wt_manager._run_git.call_count
        wt_manager.keep("no-git-call")
        assert wt_manager._run_git.call_count == call_count_before


# ---------------------------------------------------------------------------
# is_clean / has_changes
# ---------------------------------------------------------------------------

class TestIsClean:
    def test_is_clean_true(self, wt_manager):
        wt_manager.create("clean-wt")
        wt_manager._run_git = _make_git_status_mock("")
        assert wt_manager.is_clean("clean-wt") is True

    def test_is_clean_false(self, wt_manager):
        wt_manager.create("dirty-wt")
        wt_manager._run_git = _make_git_status_mock(" M src/main.py\n")
        assert wt_manager.is_clean("dirty-wt") is False

    def test_has_changes_true(self, wt_manager):
        wt_manager.create("changes-wt")
        wt_manager._run_git = _make_git_status_mock(" M file.txt\n?? new.txt\n")
        assert wt_manager.has_changes("changes-wt") is True

    def test_is_clean_not_found(self, wt_manager):
        with pytest.raises(WorktreeError, match="not found"):
            wt_manager.is_clean("no-such-wt")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class TestList:
    def test_list_all_empty(self, wt_manager):
        assert wt_manager.list_all() == []

    def test_list_all_sorted(self, wt_manager):
        # Create entries with different created_at to test sorting
        wt_manager._store.save({"name": "zzz", "path": "/tmp/zzz", "status": "active", "created_at": 3})
        wt_manager._store.save({"name": "aaa", "path": "/tmp/aaa", "status": "active", "created_at": 1})
        entries = wt_manager.list_all()
        names = [e["name"] for e in entries]
        assert names[0] == "aaa"
        assert names[1] == "zzz"

    def test_list_git_worktrees(self, wt_manager):
        wt_manager._run_git = Mock(return_value="D:\\project   abc123 [main]\n")
        lines = wt_manager.list_git_worktrees()
        assert len(lines) == 1
        assert "main" in lines[0]


# ---------------------------------------------------------------------------
# Base ref resolution
# ---------------------------------------------------------------------------

class TestBaseRef:
    def test_head_mode_uses_head(self, wt_manager):
        assert wt_manager._resolve_base_ref() == "HEAD"

    def test_fresh_mode_falls_back_to_head(self, tmp_path):
        mgr = WorktreeManager(project_root=tmp_path, base_ref="fresh")
        mgr._run_git = Mock(side_effect=WorktreeError("INTERNAL_ERROR", "no origin"))
        mgr._ensure_git_available = Mock()
        assert mgr._resolve_base_ref() == "HEAD"

    def test_fresh_mode_uses_origin_head(self, tmp_path):
        mgr = WorktreeManager(project_root=tmp_path, base_ref="fresh")
        mgr._run_git = Mock(return_value="origin/main\n")
        mgr._ensure_git_available = Mock()
        assert mgr._resolve_base_ref() == "origin/main"


# ---------------------------------------------------------------------------
# WorktreeError
# ---------------------------------------------------------------------------

class TestWorktreeError:
    def test_error_attributes(self):
        err = WorktreeError("NOT_FOUND", "no such worktree")
        assert err.code == "NOT_FOUND"
        assert err.message == "no such worktree"
        assert str(err) == "no such worktree"
