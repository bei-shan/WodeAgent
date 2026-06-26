"""AgentTeams approval file persistence tests."""

from __future__ import annotations

import json

from core.team_engine.manager import TeamManager


def test_pending_approval_is_persisted_and_restored(tmp_path):
    manager = TeamManager(project_root=tmp_path, work_executor=lambda item: {"result": "done"})
    try:
        manager.create_team("demo", members=[{"name": "lead"}])
        manager.spawn_teammate(
            "demo",
            "dev1",
            tool_policy={"allowlist": [], "denylist": ["Task"], "require_plan_approval": True},
        )

        request = manager.approval_service.create_request("demo", "dev1", "task-1", "Refactor auth")
        manager._persist_approval(request)
        approvals_path = tmp_path / ".teams" / "demo" / "approvals.json"

        assert approvals_path.exists()
        payload = json.loads(approvals_path.read_text(encoding="utf-8"))
        assert request["request_id"] in payload
        assert payload[request["request_id"]]["status"] == "pending"
    finally:
        manager.shutdown()

    restored = TeamManager(project_root=tmp_path, work_executor=lambda item: {"result": "done"})
    try:
        approvals = restored.list_plan_approvals("demo", status="pending")
        assert len(approvals) == 1
        assert approvals[0]["request_id"] == request["request_id"]
        assert approvals[0]["task_id"] == "task-1"
    finally:
        restored.shutdown()


def test_approved_dispatched_approval_is_restored_and_not_dispatched_twice(tmp_path):
    manager = TeamManager(project_root=tmp_path, work_executor=lambda item: {"result": "done"})
    try:
        manager.create_team(
            "demo",
            members=[
                {"name": "lead"},
                {"name": "dev1", "tool_policy": {"allowlist": [], "denylist": ["Task"], "require_plan_approval": True}},
            ],
        )
        task = manager.create_board_task("demo", subject="Refactor auth", description="refactor")
        task = manager.claim_next_board_task("demo", owner="dev1")
        task_id = str(task["id"])
        request = manager.approval_service.create_request("demo", "dev1", task_id, task["subject"])
        manager._persist_approval(request)

        assert manager.approval_service.apply_response("demo", "dev1", request["request_id"], True, "go")
        approved = manager.approval_service.get_request(request["request_id"])
        manager._persist_approval(approved)
        assert manager._dispatch_approved_plan_work("demo", "dev1") is True
        first_count = manager.collect_work("demo")["total"]
    finally:
        manager.shutdown()

    restored = TeamManager(project_root=tmp_path, work_executor=lambda item: {"result": "done"})
    try:
        approvals = restored.list_plan_approvals("demo", status="approved")
        assert len(approvals) == 1
        assert approvals[0]["dispatched"] is True
        assert approvals[0]["feedback"] == "go"
        assert restored._dispatch_approved_plan_work("demo", "dev1") is False
        assert restored.collect_work("demo")["total"] == first_count
    finally:
        restored.shutdown()


def test_import_state_backfills_approval_file(tmp_path):
    manager = TeamManager(project_root=tmp_path)
    try:
        manager.create_team("demo", members=[{"name": "lead"}, {"name": "dev1"}])
        request = {
            "request_id": "req_legacy",
            "team_name": "demo",
            "teammate": "dev1",
            "task_id": "task-1",
            "subject": "Legacy",
            "status": "pending",
            "feedback": "",
            "approved": None,
            "dispatched": False,
            "created_at": 1.0,
            "updated_at": 1.0,
        }
        manager.import_state({"approvals": {"demo": {"requests": [request]}}})

        approvals = manager.list_plan_approvals("demo", status="pending")
        assert [row["request_id"] for row in approvals] == ["req_legacy"]
        approvals_path = tmp_path / ".teams" / "demo" / "approvals.json"
        assert "req_legacy" in json.loads(approvals_path.read_text(encoding="utf-8"))
    finally:
        manager.shutdown()
