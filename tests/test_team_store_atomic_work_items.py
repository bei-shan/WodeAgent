"""Atomic TeamStore work queues are safe for concurrent lock-free readers."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from core.team_engine.store import TeamStore


def test_many_concurrent_writer_snapshots_never_expose_partial_json(tmp_path):
    store = TeamStore(tmp_path)
    store.create_team("demo")
    failures = []
    finished = threading.Event()

    def writer(index):
        try:
            for number in range(16):
                store.create_work_item(
                    "demo", owner="worker", title=f"{index}-{number}",
                    instruction="do work",
                )
        except Exception as exc:
            failures.append(exc)

    def reader():
        while not finished.is_set():
            try:
                rows = store.list_work_items("demo")
                assert all("work_id" in row and "status" in row for row in rows)
            except Exception as exc:
                failures.append(exc)
                return

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    consumer = threading.Thread(target=reader)
    consumer.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=8)
        assert not t.is_alive()
    finished.set()
    consumer.join(timeout=2)
    assert not consumer.is_alive()
    assert failures == []
    final = store.list_work_items("demo")
    assert len(final) == 64
    assert len({row["work_id"] for row in final}) == 64


def test_atomic_replace_error_preserves_previous_queue_and_cleans_temp(tmp_path, monkeypatch):
    store = TeamStore(tmp_path)
    store.create_team("demo")
    first = store.create_work_item("demo", owner="worker", title="one", instruction="ok")
    before = store.list_work_items("demo")

    def reject_replace(source, target):
        assert os.path.isfile(source)
        raise OSError("simulated storage error")

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "replace", reject_replace)
        with pytest.raises(OSError, match="storage error"):
            store.create_work_item("demo", owner="worker", title="two", instruction="bad")
    assert store.list_work_items("demo") == before
    work_dir = tmp_path / ".teams/demo/work_items"
    assert not list(work_dir.rglob(".work-items-*.tmp"))
