"""AgentTeams retry policy tests."""

from __future__ import annotations

import pytest

from core.team_engine.manager import TeamManager, TeamManagerError
from core.team_engine.protocol import (
    WORK_ITEM_STATUS_CANCELED,
    WORK_ITEM_STATUS_FAILED,
    WORK_ITEM_STATUS_QUEUED,
    WORK_ITEM_STATUS_RUNNING,
    WORK_ITEM_STATUS_SUCCEEDED,
)


def _create_item(manager: TeamManager, status: str):
    item = manager.store.create_work_item("demo", owner="dev1", title="work", instruction="do it")
    manager.store.update_work_item_status("demo", item["work_id"], status=status, result="old", error="boom")
    return item


@pytest.mark.parametrize("status", [WORK_ITEM_STATUS_FAILED, WORK_ITEM_STATUS_CANCELED])
def test_retry_accepts_failed_or_canceled(tmp_path, status, monkeypatch):
    manager = TeamManager(project_root=tmp_path)
    try:
        manager.create_team("demo", members=[{"name": "lead"}, {"name": "dev1"}])
        item = _create_item(manager, status)
        started = []
        monkeypatch.setattr(manager, "_start_worker", lambda team, member: started.append((team, member)))

        retried = manager.retry_failed_work("demo", item["work_id"])

        assert retried["status"] == WORK_ITEM_STATUS_QUEUED
        assert retried["error"] is None
        assert retried["result"] is None
        assert started == [("demo", "dev1")]
    finally:
        manager.shutdown()


@pytest.mark.parametrize("status", [WORK_ITEM_STATUS_QUEUED, WORK_ITEM_STATUS_RUNNING, WORK_ITEM_STATUS_SUCCEEDED])
def test_retry_rejects_non_failed_states(tmp_path, status):
    manager = TeamManager(project_root=tmp_path)
    try:
        manager.create_team("demo", members=[{"name": "lead"}, {"name": "dev1"}])
        item = _create_item(manager, status)

        with pytest.raises(TeamManagerError) as exc:
            manager.retry_failed_work("demo", item["work_id"])

        assert exc.value.code == "CONFLICT"
    finally:
        manager.shutdown()


def test_retry_unknown_work_id_returns_not_found(tmp_path):
    manager = TeamManager(project_root=tmp_path)
    try:
        manager.create_team("demo", members=[{"name": "lead"}, {"name": "dev1"}])

        with pytest.raises(TeamManagerError) as exc:
            manager.retry_failed_work("demo", "missing")

        assert exc.value.code == "NOT_FOUND"
    finally:
        manager.shutdown()
