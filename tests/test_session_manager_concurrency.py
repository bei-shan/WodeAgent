"""Single-process concurrency regression for legacy JSON session persistence.

These tests do NOT establish cross-process or multi-tenant safety.
"""

from concurrent.futures import ThreadPoolExecutor

from core.session_manager import SessionManager


def test_concurrent_catalog_creates_do_not_lose_entries(tmp_path):
    directory = str(tmp_path / "sessions")

    def create(i):
        # Different instances replicate the Web + Agent use of SessionManager.
        return SessionManager(directory).create_session(title=f"session {i}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        created = list(pool.map(create, range(80)))

    manager = SessionManager(directory)
    listed = manager.list_sessions()
    assert len(created) == len(set(created)) == 80
    assert set(created) == {item.id for item in listed}
    for sid in created:
        assert manager.load_session(sid)["history_messages"] == []


def test_overlapping_snapshot_writes_never_produce_partial_json(tmp_path):
    manager = SessionManager(str(tmp_path))
    session_id = manager.create_session()
    snapshots = [
        {"history_messages": [{"role": "user", "content": str(i) * 1000}]}
        for i in range(50)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda item: manager.save_session(session_id, item), snapshots))

    assert manager.load_session(session_id) in snapshots
    assert len(manager.list_sessions()) == 1
    assert not list(tmp_path.glob(".*.tmp"))
