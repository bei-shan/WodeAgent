"""TaskBoardStore publishes full JSON snapshots to lock-free readers."""
from __future__ import annotations

import json
import threading

import pytest

from core.team_engine.task_board_store import TaskBoardStore


def test_task_update_is_atomic_for_concurrent_readers(tmp_path):
    store = TaskBoardStore(tmp_path)
    item = store.create_task("alpha", subject="work")
    errors = []
    ready = threading.Event()

    def writer():
        ready.wait(timeout=2)
        try:
            for i in range(75):
                store.update_task("alpha", item["id"], description=f"state-{i}")
        except BaseException as exc:
            errors.append(exc)

    def reader():
        ready.wait(timeout=2)
        try:
            for _ in range(350):
                task = store.get_task("alpha", item["id"])
                assert task["id"] == item["id"]
                assert task["subject"] == "work"
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    for t in threads:
        t.start()
    ready.set()
    for t in threads:
        t.join(timeout=8)
        assert not t.is_alive()
    assert not errors, errors
    assert store.get_task("alpha", item["id"])["description"] == "state-74"


def test_failed_replace_leaves_previous_snapshot_and_cleans_temp_file(tmp_path, monkeypatch):
    store = TaskBoardStore(tmp_path)
    item = store.create_task("alpha", subject="work", description="before")
    import core.team_engine.task_board_store as module

    def fail_replace(src, dst):
        assert json.loads(open(src, encoding="utf-8").read())["description"] == "after"
        raise OSError("simulated replace failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(module.os, "replace", fail_replace)
        with pytest.raises(OSError, match="replace failure"):
            store.update_task("alpha", item["id"], description="after")
    current = store.get_task("alpha", item["id"])
    assert current["description"] == "before"
    assert not list((tmp_path / ".tasks" / "alpha").glob("*.tmp"))
