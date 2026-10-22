"""Snapshot vs log consistency: audit states and one-directional repair."""
from __future__ import annotations

import json

from core.session_log import SessionLog
from core.session_manager import SessionManager


def _snapshot(*messages: tuple[str, str]) -> dict:
    return {
        "version": 2,
        "schema_version": 2,
        "history_messages": [
            {"role": role, "content": content, "message_id": f"m{index}"}
            for index, (role, content) in enumerate(messages, start=1)
        ],
    }


def test_clean_session_audits_ok_and_is_not_rewritten(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("clean")
    manager.save_session(session_id, _snapshot(("user", "hi"), ("assistant", "there")))
    log_bytes = SessionLog(tmp_path, session_id).path.read_bytes()

    assert manager.audit_session(session_id)["state"] == "ok"
    assert manager.reconcile_sessions() == {"ok": 1}
    assert SessionLog(tmp_path, session_id).path.read_bytes() == log_bytes


def test_log_behind_is_repaired_from_the_snapshot(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("behind")
    manager.save_session(session_id, _snapshot(("user", "one")))
    # Snapshot advanced outside save_session, so the log lags.
    (tmp_path / f"{session_id}.json").write_text(
        json.dumps(_snapshot(("user", "one"), ("assistant", "two"), ("user", "three"))),
        encoding="utf-8",
    )

    report = manager.audit_session(session_id)
    assert report["state"] == "log_behind"
    assert report["snapshot_messages"] == 3 and report["log_events"] == 1

    assert manager.reconcile_sessions() == {"repaired": 1}
    repaired = SessionLog(tmp_path, session_id)
    assert [m["content"] for m in repaired.derive_messages()] == ["one", "two", "three"]
    assert manager.audit_session(session_id)["state"] == "ok"


def test_diverged_log_is_rewritten_from_the_snapshot(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("diverged")
    manager.save_session(session_id, _snapshot(("user", "a"), ("assistant", "b")))
    # Same length, different content: the snapshot is authoritative.
    SessionLog(tmp_path, session_id).rewrite(_snapshot(("user", "x"), ("assistant", "y")))

    assert manager.audit_session(session_id)["state"] == "diverged"
    assert manager.reconcile_sessions() == {"repaired": 1}
    assert [m["content"] for m in SessionLog(tmp_path, session_id).derive_messages()] == ["a", "b"]


def test_log_ahead_is_left_alone(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("ahead")
    manager.save_session(session_id, _snapshot(("user", "one")))
    SessionLog(tmp_path, session_id).sync(_snapshot(("user", "one"), ("assistant", "two")))
    before = SessionLog(tmp_path, session_id).path.read_bytes()

    assert manager.audit_session(session_id)["state"] == "log_ahead"
    assert manager.reconcile_sessions() == {"log_ahead": 1}
    assert SessionLog(tmp_path, session_id).path.read_bytes() == before


def test_snapshot_only_and_log_only_states(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    # A brand-new session owns an empty snapshot *and* an empty log.
    fresh = manager.create_session("fresh")
    assert manager.audit_session(fresh)["state"] == "ok"
    assert SessionLog(tmp_path, fresh).exists()

    logged = manager.create_session("log only")
    manager.save_session(logged, _snapshot(("user", "x")))
    (tmp_path / f"{logged}.json").unlink()
    report = manager.audit_session(logged)
    assert report["state"] == "log_only"
    assert report["log_events"] == 1


def test_snapshot_without_log_is_reported_as_snapshot_only(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    orphan = "abcdefabcdef"
    (tmp_path / f"{orphan}.json").write_text(json.dumps(_snapshot(("user", "a"))), encoding="utf-8")
    assert manager.audit_session(orphan)["state"] == "snapshot_only"
    assert manager.reconcile_sessions() == {"snapshot_only": 1}


def test_repair_never_touches_the_snapshot_or_its_tree(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    session_id = manager.create_session("tree")
    snapshot = _snapshot(("user", "a"))
    snapshot["history_entries"] = [
        {"type": "message", "id": "m1", "parentId": None, "role": "user", "content": "a"},
        {"type": "message", "id": "m2", "parentId": "m1", "role": "assistant", "content": "b"},
    ]
    manager.save_session(session_id, snapshot)
    SessionLog(tmp_path, session_id).rewrite(_snapshot(("user", "different")))
    before = (tmp_path / f"{session_id}.json").read_bytes()

    assert manager.reconcile_sessions() == {"repaired": 1}
    assert (tmp_path / f"{session_id}.json").read_bytes() == before
    assert len(manager._read_snapshot_raw(session_id)["history_entries"]) == 2


def test_missing_session_is_reported_not_repaired(tmp_path):
    manager = SessionManager(sessions_dir=str(tmp_path))
    assert manager.audit_session("0123456789ab")["state"] == "missing"
    assert manager.reconcile_sessions() == {}
