"""Tests for multi-session management."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.session_manager import SessionManager, SessionInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snapshot(history: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "history_messages": history or [
            {"role": "user", "content": "Hello, fix the login bug"},
            {"role": "assistant", "content": "I'll help with that."},
        ],
    }


# ---------------------------------------------------------------------------
# Core tests
# ---------------------------------------------------------------------------

class TestCreateSession:
    """Test session creation."""

    def test_create_returns_id(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        assert len(sid) == 12
        assert all(c in "0123456789abcdef" for c in sid)

    def test_create_generates_unique_ids(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        ids = {mgr.create_session() for _ in range(10)}
        assert len(ids) == 10

    def test_create_writes_snapshot_file(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        assert (tmp_path / f"{sid}.json").exists()

    def test_create_with_custom_title(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session(title="My custom title")
        sessions = mgr.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].title == "My custom title"

    def test_create_empty_title_generates_placeholder(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        sessions = mgr.list_sessions()
        assert sessions[0].title.startswith("New session")


class TestSaveAndLoad:
    """Test session save and load."""

    def test_save_updates_index(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        snapshot = _make_snapshot()
        mgr.save_session(sid, snapshot)
        sessions = mgr.list_sessions()
        assert sessions[0].message_count == 2
        assert "login bug" in sessions[0].title
        assert "Hello, fix" in sessions[0].preview

    def test_load_returns_snapshot(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        snapshot = _make_snapshot()
        mgr.save_session(sid, snapshot)
        loaded = mgr.load_session(sid)
        assert loaded is not None
        assert loaded["version"] == 1
        assert len(loaded["history_messages"]) == 2

    def test_load_nonexistent_returns_none(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        assert mgr.load_session("nonexistent") is None

    def test_title_extracted_from_first_user_message(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        snapshot = _make_snapshot([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Please refactor the auth module to use JWT tokens for all endpoints"},
        ])
        mgr.save_session(sid, snapshot)
        sessions = mgr.list_sessions()
        assert "refactor the auth module" in sessions[0].title

    def test_title_truncated_to_max_len(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path), auto_title_max_len=20)
        sid = mgr.create_session()
        snapshot = _make_snapshot([
            {"role": "user", "content": "This is a very long message that should be truncated for the title"},
        ])
        mgr.save_session(sid, snapshot)
        sessions = mgr.list_sessions()
        assert len(sessions[0].title) <= 20


class TestListSessions:
    """Test session listing."""

    def test_list_empty(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        assert mgr.list_sessions() == []

    def test_list_sorted_by_modified_desc(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        s1 = mgr.create_session(title="First")
        s2 = mgr.create_session(title="Second")
        mgr.save_session(s1, _make_snapshot())
        mgr.save_session(s2, _make_snapshot())
        sessions = mgr.list_sessions()
        assert sessions[0].title == "Second"
        assert sessions[1].title == "First"

    def test_get_session_by_id(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session(title="test session")
        info = mgr.get_session(sid)
        assert info is not None
        assert info.title == "test session"

    def test_get_session_nonexistent(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        assert mgr.get_session("nonexistent") is None


class TestResolveIdentifier:
    """Test session identifier resolution."""

    def test_exact_id_match(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        assert mgr.resolve_identifier(sid) == sid

    def test_numeric_index(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        s1 = mgr.create_session()
        s2 = mgr.create_session()
        mgr.save_session(s1, _make_snapshot())
        mgr.save_session(s2, _make_snapshot())
        # Index 1 = most recent (s2), index 2 = s1
        assert mgr.resolve_identifier("1") == s2
        assert mgr.resolve_identifier("2") == s1

    def test_numeric_index_out_of_range(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        mgr.create_session()
        assert mgr.resolve_identifier("99") is None

    def test_prefix_match(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        prefix = sid[:6]
        assert mgr.resolve_identifier(prefix) == sid

    def test_prefix_too_short(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        assert mgr.resolve_identifier(sid[:2]) is None

    def test_empty_identifier(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        assert mgr.resolve_identifier("") is None
        assert mgr.resolve_identifier("  ") is None


class TestRename:
    """Test session renaming."""

    def test_rename_updates_title(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session(title="old title")
        assert mgr.rename_session(sid, "new title") is True
        info = mgr.get_session(sid)
        assert info.title == "new title"

    def test_rename_nonexistent(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        assert mgr.rename_session("nonexistent", "x") is False


class TestDelete:
    """Test session deletion."""

    def test_delete_removes_snapshot_and_index(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr.create_session()
        mgr.save_session(sid, _make_snapshot())
        assert mgr.delete_session(sid) is True
        assert mgr.load_session(sid) is None
        assert mgr.get_session(sid) is None

    def test_delete_nonexistent(self, tmp_path):
        mgr = SessionManager(sessions_dir=str(tmp_path))
        assert mgr.delete_session("nonexistent") is False


class TestIndexPersistence:
    """Test that index.json is correctly persisted across instances."""

    def test_index_survives_reload(self, tmp_path):
        mgr1 = SessionManager(sessions_dir=str(tmp_path))
        sid = mgr1.create_session(title="persistent")
        mgr1.save_session(sid, _make_snapshot())

        # Create a new manager pointing to the same directory.
        mgr2 = SessionManager(sessions_dir=str(tmp_path))
        sessions = mgr2.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].id == sid
        assert sessions[0].title == "persistent"

    def test_corrupted_index_handled_gracefully(self, tmp_path):
        (tmp_path / "index.json").write_text("not valid json {{{")
        mgr = SessionManager(sessions_dir=str(tmp_path))
        assert mgr.list_sessions() == []
