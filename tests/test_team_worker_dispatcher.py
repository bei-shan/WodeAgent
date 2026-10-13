"""Worker inbox/task dispatch is an independent Team ownership boundary."""
from __future__ import annotations

import ast
from pathlib import Path

from core.team_engine.manager import TeamManager
from core.team_engine.worker_dispatcher import WorkerDispatchService


def test_message_to_work_dispatch_deduplicates_by_source_id(tmp_path):
    manager = TeamManager(project_root=tmp_path)
    try:
        manager.create_team("demo", members=[{"name": "lead"}])
        message = {
            "message_id": "msg-123", "from": "lead", "type": "message",
            "summary": "Review", "text": "Inspect authorization checks",
        }
        assert manager._enqueue_message_work("demo", "lead", message) is True
        assert manager._enqueue_message_work("demo", "lead", message) is False
        work = manager.store.list_work_items("demo", owner="lead")
        assert len(work) == 1
        assert work[0]["payload"]["source_message_id"] == "msg-123"
        assert work[0]["instruction"] == "Inspect authorization checks"
    finally:
        manager.shutdown()


def test_dispatcher_consumes_inbox_once_and_persists_processed_status(tmp_path):
    manager = TeamManager(project_root=tmp_path)
    try:
        manager.create_team("demo", members=[{"name": "lead"}])
        row = manager.send_message("demo", "lead", "lead", "Check logs", summary="Review")
        # The lead has no live worker; explicit poll follows the same path as
        # WorkerSupervisor's poll callback.
        assert manager._process_member_inbox("demo", "lead") is True
        status = manager.get_status("demo")
        assert status["message_counts"]["processed"] == 1
        assert row["message_id"] in manager._processed_by_member[("demo", "lead")]
        assert manager._inbox_cursor[("demo", "lead")] == 1
        count = len(manager.store.list_work_items("demo", owner="lead"))
        manager._process_member_inbox("demo", "lead")
        assert len(manager.store.list_work_items("demo", owner="lead")) == count
    finally:
        manager.shutdown()


def test_manager_adapter_routes_poll_and_dispatch_to_same_service(tmp_path, monkeypatch):
    manager = TeamManager(project_root=tmp_path)
    try:
        calls = []
        monkeypatch.setattr(WorkerDispatchService, "_process_member_inbox",
                            lambda self, team, member: calls.append((team, member)) or True)
        assert manager._process_member_inbox("demo", "dev1") is True
        assert calls == [("demo", "dev1")]
        assert manager.worker_dispatcher.owner is manager
    finally:
        manager.shutdown()


def test_dispatcher_does_not_import_manager_or_transport():
    source = (Path(__file__).resolve().parents[1] /
              "core/team_engine/worker_dispatcher.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                ("core.team_engine.manager", "agents", "desktop", "fastapi", "tui"))
