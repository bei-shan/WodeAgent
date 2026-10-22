"""Session schema migrations: v1 snapshot-only -> v2 snapshot + JSONL log."""
from __future__ import annotations

import json

from core.session_log import SessionLog
from core.session_migrations import LATEST, migrate_snapshot, needs_migration, snapshot_version
from core.session_manager import SessionManager


def _legacy_snapshot(tmp_path, session_id: str, messages: list[tuple[str, str]]) -> None:
    path = tmp_path / f"{session_id}.json"
    path.write_text(json.dumps({
        "version": 1,
        "history_messages": [
            {"role": role, "content": content, "message_id": f"m{index}"}
            for index, (role, content) in enumerate(messages, start=1)
        ],
    }), encoding="utf-8")


def test_legacy_snapshot_is_migrated_on_load(tmp_path):
    session_id = "aaaaaaaaaaaa"
    _legacy_snapshot(tmp_path, session_id, [("user", "old session"), ("assistant", "ok")])

    manager = SessionManager(sessions_dir=str(tmp_path))
    loaded = manager.load_session(session_id)
    assert loaded is not None
    assert loaded["schema_version"] == LATEST
    assert len(loaded["history_messages"]) == 2

    # The log now exists and derives the same history.
    log = SessionLog(tmp_path, session_id)
    assert log.exists()
    assert log.read_header()["schema"] == LATEST
    assert [m["content"] for m in log.derive_messages()] == ["old session", "ok"]


def test_migration_is_idempotent(tmp_path):
    session_id = "bbbbbbbbbbbb"
    _legacy_snapshot(tmp_path, session_id, [("user", "hi")])
    manager = SessionManager(sessions_dir=str(tmp_path))

    manager.load_session(session_id)
    snapshot_path = tmp_path / f"{session_id}.json"
    first = snapshot_path.read_bytes()
    log_bytes = SessionLog(tmp_path, session_id).path.read_bytes()

    manager.load_session(session_id)
    assert snapshot_path.read_bytes() == first
    assert SessionLog(tmp_path, session_id).path.read_bytes() == log_bytes


def test_current_snapshot_is_left_alone(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("current")
    manager.save_session(session_id, {
        "version": 2, "schema_version": 2,
        "history_messages": [{"role": "user", "content": "x"}],
    })
    before = (tmp_path / f"{session_id}.json").read_bytes()

    assert manager.load_session(session_id) is not None
    assert (tmp_path / f"{session_id}.json").read_bytes() == before


def test_migrate_sessions_stamps_every_legacy_snapshot(tmp_path):
    _legacy_snapshot(tmp_path, "cccccccccccc", [("user", "one")])
    _legacy_snapshot(tmp_path, "dddddddddddd", [])
    manager = SessionManager(sessions_dir=str(tmp_path))

    result = manager.migrate_sessions()
    assert result["migrated"] == 2
    for session_id in ("cccccccccccc", "dddddddddddd"):
        data = json.loads((tmp_path / f"{session_id}.json").read_text(encoding="utf-8"))
        assert data["schema_version"] == LATEST

    assert manager.migrate_sessions() == {"migrated": 0}


def test_migrate_snapshot_tolerates_missing_fields():
    migrated = migrate_snapshot({"history_messages": [{"role": "user", "content": "x"}]})
    assert migrated["schema_version"] == LATEST
    assert migrated["history_entries"] == []
    assert migrated["cursor_id"] is None
    assert migrated["system_messages"] == []
    assert migrated["version"] == 1


def test_snapshot_version_handles_garbage():
    assert snapshot_version({"schema_version": "2"}) == 2
    assert snapshot_version({"schema_version": "nope"}) == 0
    assert snapshot_version({}) == 0
    assert needs_migration({"schema_version": LATEST}) is False
    assert needs_migration({}) is True
