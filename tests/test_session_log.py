"""Append-only session log: writing, deriving, recovery and reconciliation."""
from __future__ import annotations

import json

import pytest

from core.session_log import LOG_SUFFIX, SessionLog
from core.session_manager import SessionManager


def _snapshot(*messages: tuple[str, str]) -> dict:
    return {
        "version": 1,
        "history_messages": [
            {"role": role, "content": content, "message_id": f"m{index}"}
            for index, (role, content) in enumerate(messages, start=1)
        ],
    }


def test_save_writes_header_and_derives_messages(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("log test")
    manager.save_session(session_id, _snapshot(("user", "hi"), ("assistant", "hello")))

    log = SessionLog(tmp_path, session_id)
    assert log.exists()
    header = log.read_header()
    assert header["schema"] == 2
    assert header["session_id"] == session_id
    assert [m["content"] for m in log.derive_messages()] == ["hi", "hello"]
    assert [e["id"] for e in log.derive_entries()] == ["m1", "m2"]


def test_second_save_appends_only_new_messages(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("incremental")
    manager.save_session(session_id, _snapshot(("user", "one")))
    manager.save_session(
        session_id, _snapshot(("user", "one"), ("assistant", "two"), ("user", "three")),
    )

    log = SessionLog(tmp_path, session_id)
    lines = [line for line in log.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1 + 3  # header + three events, no duplicate of "one"
    assert [m["content"] for m in log.derive_messages()] == ["one", "two", "three"]


def test_shrinking_history_republishes_a_generation(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("fork")
    manager.save_session(
        session_id, _snapshot(("user", "a"), ("assistant", "b"), ("user", "c")),
    )
    manager.save_session(session_id, _snapshot(("user", "a")))

    log = SessionLog(tmp_path, session_id)
    assert [m["content"] for m in log.derive_messages()] == ["a"]


def test_torn_tail_is_ignored_and_healed_on_next_save(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("torn")
    manager.save_session(session_id, _snapshot(("user", "kept")))
    log = SessionLog(tmp_path, session_id)
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"message","seq":9,"role":"user"')  # crashed mid-write

    assert [m["content"] for m in log.derive_messages()] == ["kept"]

    manager.save_session(session_id, _snapshot(("user", "kept"), ("assistant", "new")))
    healed = SessionLog(tmp_path, session_id).derive_messages()
    assert [m["content"] for m in healed] == ["kept", "new"]


def test_lost_snapshot_is_rebuilt_from_the_log(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("recover")
    manager.save_session(session_id, _snapshot(("user", "remember me")))
    (tmp_path / f"{session_id}.json").unlink()

    recovered = manager.load_session(session_id)
    assert recovered is not None
    assert recovered["history_messages"][0]["content"] == "remember me"


def test_delete_removes_the_log(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("bye")
    manager.save_session(session_id, _snapshot(("user", "x")))
    assert (tmp_path / f"{session_id}{LOG_SUFFIX}").exists()

    assert manager.delete_session(session_id) is True
    assert not (tmp_path / f"{session_id}{LOG_SUFFIX}").exists()


def test_reconcile_indexes_a_log_only_session(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("log only")
    manager.save_session(session_id, _snapshot(("user", "survives")))
    (tmp_path / f"{session_id}.json").unlink()
    (tmp_path / "index.json").unlink()  # lose both caches; the log is the truth

    fresh = SessionManager(sessions_dir=str(tmp_path))
    sessions = fresh.list_sessions()
    assert [item.id for item in sessions] == [session_id]
    assert sessions[0].message_count == 1
    assert sessions[0].title.startswith("survives")


def test_log_records_reasoning_and_metadata(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("meta")
    manager.save_session(session_id, {
        "history_messages": [
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "why",
                "metadata": {"step": 2},
                "message_id": "m1",
                "parent_id": None,
            }
        ]
    })
    events = SessionLog(tmp_path, session_id).read_events()
    assert events[0]["reasoning_content"] == "why"
    assert events[0]["metadata"] == {"step": 2}
    assert json.loads(json.dumps(events[0]))["seq"] == 1


def test_cursor_state_is_written_and_fallback_still_appends(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("cursor state")
    manager.save_session(session_id, _snapshot(("user", "a"), ("assistant", "b")))
    log = SessionLog(tmp_path, session_id)
    state = json.loads(log.state_path.read_text(encoding="utf-8"))
    assert state["events"] == 2
    assert state["size"] == log.path.stat().st_size

    # Losing the cursor only costs a rescan; appends must stay correct.
    log.state_path.unlink()
    manager.save_session(
        session_id, _snapshot(("user", "a"), ("assistant", "b"), ("user", "c")),
    )
    lines = [line for line in log.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1 + 3
    assert [m["content"] for m in log.derive_messages()] == ["a", "b", "c"]


def test_cursor_events_track_and_deduplicate(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("cursor")
    snapshot = _snapshot(("user", "hi"), ("assistant", "there"))
    snapshot["cursor_id"] = "m2"
    manager.save_session(session_id, snapshot)

    log = SessionLog(tmp_path, session_id)
    assert log.read_cursor() == "m2"
    assert log.to_snapshot()["cursor_id"] == "m2"
    cursor_lines = [
        line for line in log.path.read_text(encoding="utf-8").splitlines()
        if '"type": "cursor"' in line
    ]
    assert len(cursor_lines) == 1

    manager.save_session(session_id, snapshot)  # unchanged cursor: no new event
    assert len([line for line in log.path.read_text(encoding="utf-8").splitlines()
                if '"type": "cursor"' in line]) == 1

    snapshot["cursor_id"] = "m1"
    manager.save_session(session_id, snapshot)
    assert SessionLog(tmp_path, session_id).read_cursor() == "m1"


def test_load_repairs_a_log_that_lags_the_snapshot(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("lagging")
    manager.save_session(session_id, _snapshot(("user", "one")))

    # Snapshot advanced on disk without going through save_session.
    (tmp_path / f"{session_id}.json").write_text(
        json.dumps(_snapshot(("user", "one"), ("assistant", "two"), ("user", "three"))),
        encoding="utf-8",
    )
    loaded = manager.load_session(session_id)
    assert loaded is not None
    assert len(loaded["history_messages"]) == 3
    assert [m["content"] for m in SessionLog(tmp_path, session_id).derive_messages()] == [
        "one", "two", "three",
    ]


def test_delete_removes_log_and_cursor_state(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("bye")
    manager.save_session(session_id, _snapshot(("user", "x")))
    log = SessionLog(tmp_path, session_id)
    assert log.state_path.exists()

    assert manager.delete_session(session_id) is True
    assert not log.path.exists()
    assert not log.state_path.exists()


# ---------------------------------------------------------------------------
# Log-first reads (B2c)
# ---------------------------------------------------------------------------
def test_log_ahead_of_snapshot_wins_on_load(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("ahead")
    manager.save_session(session_id, _snapshot(("user", "one"), ("assistant", "two")))

    # Simulate a crash after the log append but before the snapshot write.
    ahead = _snapshot(("user", "one"), ("assistant", "two"), ("user", "three"))
    ahead["cursor_id"] = "m3"
    SessionLog(tmp_path, session_id).sync(ahead)

    loaded = manager.load_session(session_id)
    assert loaded is not None
    assert [m["content"] for m in loaded["history_messages"]] == ["one", "two", "three"]
    assert len(loaded["history_entries"]) == 3
    assert loaded["cursor_id"] == "m3"


def test_snapshot_write_failure_still_leaves_a_readable_log(tmp_path, monkeypatch):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("crash")

    def boom(session_id: str, snapshot: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_write_snapshot", boom)
    with pytest.raises(OSError):
        manager.save_session(session_id, _snapshot(("user", "survived")))

    monkeypatch.undo()
    loaded = manager.load_session(session_id)
    assert loaded is not None
    assert [m["content"] for m in loaded["history_messages"]] == ["survived"]


def test_branch_shrink_is_not_overridden_by_the_older_log(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("branch")
    manager.save_session(
        session_id, _snapshot(("user", "a"), ("assistant", "b"), ("user", "c")),
    )
    manager.save_session(session_id, _snapshot(("user", "a")))

    loaded = manager.load_session(session_id)
    assert loaded is not None
    assert [m["content"] for m in loaded["history_messages"]] == ["a"]
    assert len(SessionLog(tmp_path, session_id).derive_messages()) == 1


def test_shrinking_history_records_a_branch_point(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("rewind")
    manager.save_session(
        session_id, _snapshot(("user", "a"), ("assistant", "b"), ("user", "c")),
    )
    manager.save_session(session_id, _snapshot(("user", "a")))

    log = SessionLog(tmp_path, session_id)
    branches = log.read_branches()
    assert len(branches) == 1
    assert branches[0]["from"] == "m1"
    assert branches[0]["from_index"] == 1
    assert branches[0]["from_length"] == 3
    assert branches[0]["to_length"] == 1

    # The record is audit history: later appends must neither drop nor replay it.
    manager.save_session(session_id, _snapshot(("user", "a"), ("assistant", "b2")))
    reloaded = SessionLog(tmp_path, session_id)
    assert len(reloaded.read_branches()) == 1
    assert [m["content"] for m in reloaded.derive_messages()] == ["a", "b2"]
