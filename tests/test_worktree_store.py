"""WorktreeStore unit tests.

Run:
    python -m pytest tests/test_worktree_store.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.worktree.store import WorktreeStore


class TestWorktreeStore:
    """Tests for WorktreeStore index persistence."""

    def test_load_empty_store(self, tmp_path: Path):
        store = WorktreeStore(tmp_path)
        assert store.load_all() == []

    def test_save_and_load_single_entry(self, tmp_path: Path):
        store = WorktreeStore(tmp_path)
        entry = {"name": "test-wt", "path": "/tmp/test", "status": "active"}
        store.save(entry)
        entries = store.load_all()
        assert len(entries) == 1
        assert entries[0]["name"] == "test-wt"
        assert entries[0]["status"] == "active"

    def test_save_updates_existing_by_name(self, tmp_path: Path):
        store = WorktreeStore(tmp_path)
        store.save({"name": "wt-a", "path": "/tmp/a", "status": "active"})
        store.save({"name": "wt-a", "path": "/tmp/a", "status": "removed"})
        entries = store.load_all()
        assert len(entries) == 1
        assert entries[0]["status"] == "removed"

    def test_save_multiple_entries(self, tmp_path: Path):
        store = WorktreeStore(tmp_path)
        store.save({"name": "wt-a", "path": "/tmp/a", "status": "active"})
        store.save({"name": "wt-b", "path": "/tmp/b", "status": "active"})
        store.save({"name": "wt-c", "path": "/tmp/c", "status": "kept"})
        entries = store.load_all()
        assert len(entries) == 3
        names = {e["name"] for e in entries}
        assert names == {"wt-a", "wt-b", "wt-c"}

    def test_find_existing_entry(self, tmp_path: Path):
        store = WorktreeStore(tmp_path)
        store.save({"name": "target", "path": "/tmp/target", "status": "active"})
        store.save({"name": "other", "path": "/tmp/other", "status": "active"})
        found = store.find("target")
        assert found is not None
        assert found["name"] == "target"
        assert found["path"] == "/tmp/target"

    def test_find_nonexistent(self, tmp_path: Path):
        store = WorktreeStore(tmp_path)
        assert store.find("missing") is None

    def test_find_returns_copy_not_reference(self, tmp_path: Path):
        """Verify find() returns a copy, not the internal reference."""
        store = WorktreeStore(tmp_path)
        store.save({"name": "wt", "status": "active"})
        found = store.find("wt")
        found["status"] = "modified"
        # Re-load - should still be active
        reloaded = store.find("wt")
        assert reloaded["status"] == "active"

    def test_load_all_sorted_by_created_at(self, tmp_path: Path):
        store = WorktreeStore(tmp_path)
        store.save({"name": "wt-c", "created_at": 3})
        store.save({"name": "wt-a", "created_at": 1})
        store.save({"name": "wt-b", "created_at": 2})
        entries = store.load_all()
        assert [e["name"] for e in entries] == ["wt-a", "wt-b", "wt-c"]

    def test_persists_to_disk(self, tmp_path: Path):
        store = WorktreeStore(tmp_path)
        store.save({"name": "persistent", "status": "active"})
        # Create a new store pointing to the same directory
        store2 = WorktreeStore(tmp_path)
        entries = store2.load_all()
        assert len(entries) == 1
        assert entries[0]["name"] == "persistent"

    def test_corrupt_index_file_returns_empty(self, tmp_path: Path):
        """Malformed JSON should be handled gracefully."""
        index_path = tmp_path / "index.json"
        index_path.write_text("{broken json", encoding="utf-8")
        store = WorktreeStore(tmp_path)
        assert store.load_all() == []

    def test_missing_worktrees_key_returns_empty(self, tmp_path: Path):
        """Valid JSON without 'worktrees' key should return empty list."""
        index_path = tmp_path / "index.json"
        index_path.write_text('{"other": [1, 2, 3]}', encoding="utf-8")
        store = WorktreeStore(tmp_path)
        entries = store.load_all()
        assert entries == []
