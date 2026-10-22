"""index.json is a cache: reconciliation repairs it from the snapshots."""
from __future__ import annotations

import json

from core.session_manager import SessionManager


def _write_snapshot(directory, session_id: str, messages: list[dict]) -> None:
    path = directory / f"{session_id}.json"
    path.write_text(
        json.dumps({"version": 1, "history_messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_orphan_snapshot_is_indexed_with_derived_metadata(tmp_path):
    _write_snapshot(
        tmp_path,
        "aaaaaaaaaaaa",
        [
            {"role": "user", "content": "Fix the login redirect loop please"},
            {"role": "assistant", "content": "ok"},
        ],
    )
    manager = SessionManager(sessions_dir=str(tmp_path))

    sessions = manager.list_sessions()
    assert [item.id for item in sessions] == ["aaaaaaaaaaaa"]
    info = sessions[0]
    assert info.title.startswith("Fix the login redirect loop")
    assert info.message_count == 2
    assert info.preview.startswith("Fix the login redirect loop")
    # Persisted for the next reader.
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))[0]["id"] == "aaaaaaaaaaaa"


def test_entry_whose_snapshot_vanished_is_dropped(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    ghost = manager.create_session("ghost")
    (tmp_path / f"{ghost}.json").unlink()

    fresh = SessionManager(sessions_dir=str(tmp_path))
    assert fresh.list_sessions() == []
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8")) == []


def test_reconcile_is_idempotent(tmp_path):
    _write_snapshot(tmp_path, "bbbbbbbbbbbb", [{"role": "user", "content": "hello"}])
    manager = SessionManager(sessions_dir=str(tmp_path))
    manager.reconcile_index()
    first = (tmp_path / "index.json").read_bytes()

    again = manager.reconcile_index()
    assert again == {"added": 0, "removed": 0}
    assert (tmp_path / "index.json").read_bytes() == first


def test_corrupt_snapshot_is_skipped_without_crashing(tmp_path):
    (tmp_path / "cccccccccccc.json").write_text("{not json", encoding="utf-8")
    manager = SessionManager(sessions_dir=str(tmp_path))
    assert manager.list_sessions() == []


def test_turn_sidecars_and_index_are_not_treated_as_sessions(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("real")
    (tmp_path / f"{session_id}.turn.json").write_text("{}", encoding="utf-8")

    fresh = SessionManager(sessions_dir=str(tmp_path))
    assert [item.id for item in fresh.list_sessions()] == [session_id]


def test_reconcile_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_INDEX_RECONCILE", "false")
    _write_snapshot(tmp_path, "dddddddddddd", [{"role": "user", "content": "hello"}])
    manager = SessionManager(sessions_dir=str(tmp_path))
    assert manager.list_sessions() == []


def test_reconcile_preserves_existing_entries(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    kept = manager.create_session("Keep me")
    _write_snapshot(tmp_path, "eeeeeeeeeeee", [{"role": "user", "content": "New one"}])

    fresh = SessionManager(sessions_dir=str(tmp_path))
    titles = {item.id: item.title for item in fresh.list_sessions()}
    assert titles[kept] == "Keep me"
    assert titles["eeeeeeeeeeee"] == "New one"
