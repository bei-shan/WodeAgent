"""Checkpoint ownership, state compatibility and independent worker lifecycle."""
from __future__ import annotations

import ast
from pathlib import Path

from core.team_engine.manager import TeamManager
from core.team_engine.checkpoint import TeamCheckpointService


def test_checkpoint_roundtrip_inbox_and_processed_message_state(tmp_path):
    manager = TeamManager(project_root=tmp_path)
    try:
        manager.create_team("demo", members=[{"name": "lead"}])
        manager.import_state({
            "processed_messages": {"demo|dev": ["m1", "m2"]},
            "inbox_cursors": {"demo|dev": 7},
        })
        state = manager.export_state()
        assert set(state["processed_messages"]["demo|dev"]) == {"m1", "m2"}
        assert state["inbox_cursors"]["demo|dev"] == 7
        assert "demo" in state["teams"]

        # Idempotent restore: message IDs are sets; no duplicate worker startup.
        manager.import_state(state)
        manager.import_state(state)
        assert manager._processed_by_member[("demo", "dev")] == {"m1", "m2"}
        assert manager._inbox_cursor[("demo", "dev")] == 7
        assert manager.worker_supervisor.active_workers("demo") == []
    finally:
        manager.shutdown()


def test_checkpoint_projection_is_framework_independent():
    root = Path(__file__).resolve().parents[1]
    source = (root / "core/team_engine/checkpoint.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("agents", "desktop", "tui", "fastapi"))
    assert "from .manager import TeamManager" not in source


def test_manager_routes_export_and_restore_through_single_owner(tmp_path, monkeypatch):
    manager = TeamManager(project_root=tmp_path)
    try:
        called = []
        monkeypatch.setattr(TeamCheckpointService, "snapshot", lambda self: called.append("snapshot") or {"teams": {}})
        monkeypatch.setattr(TeamCheckpointService, "restore", lambda self, data: called.append(("restore", data)))
        assert manager.export_state() == {"teams": {}}
        manager.import_state({"test": 1})
        assert called == ["snapshot", ("restore", {"test": 1})]
    finally:
        manager.shutdown()
