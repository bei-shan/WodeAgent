"""BackgroundTaskRunner unit tests.

Run:
    python -m pytest tests/test_background_task.py -v
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.background_task import BackgroundTaskRunner


class TestBackgroundTaskRunner:
    """Tests for BackgroundTaskRunner lifecycle and persistence."""

    def test_launch_and_wait_for_completion(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)

        def _work() -> tuple:
            return "all done", {"Read": 2, "Grep": 1}

        runner.launch("task-1", _work, "test task")
        # Poll for completion (daemon thread, should finish quickly)
        for _ in range(50):
            if runner.get_status("task-1") == "completed":
                break
            time.sleep(0.01)
        assert runner.get_status("task-1") == "completed"

    def test_launch_and_get_result(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)

        def _work() -> tuple:
            return "result text", {"Read": 3}

        runner.launch("task-2", _work, "desc")
        for _ in range(50):
            result = runner.get_result("task-2")
            if result is not None:
                break
            time.sleep(0.01)

        assert result is not None
        assert result["status"] == "completed"
        assert result["result"] == "result text"
        assert result["tool_usage"] == {"Read": 3}
        assert "finished_at" in result

    def test_launch_failed_task(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)

        def _fail() -> tuple:
            raise RuntimeError("boom")

        runner.launch("task-fail", _fail, "will fail")
        for _ in range(50):
            if runner.get_status("task-fail") == "failed":
                break
            time.sleep(0.01)

        assert runner.get_status("task-fail") == "failed"
        result = runner.get_result("task-fail")
        assert result is not None
        assert result["status"] == "failed"
        assert "boom" in result.get("result", "")

    def test_get_status_not_found(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)
        assert runner.get_status("nonexistent") == "not_found"

    def test_get_result_not_finished_returns_none(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)
        # Never launched → no result file
        assert runner.get_result("no-such") is None

    def test_get_status_running(self, tmp_path: Path):
        import threading
        barrier = threading.Event()

        def _slow() -> tuple:
            barrier.wait()
            return "done", {}

        runner.launch("slow-task", _slow, "slow desc")
        assert runner.get_status("slow-task") == "running"
        barrier.set()  # let it finish
        for _ in range(50):
            if runner.get_status("slow-task") == "completed":
                break
            time.sleep(0.01)

    def test_list_all(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)

        def _fast() -> tuple:
            return "ok", {}
        runner.launch("a", _fast, "task a")
        runner.launch("b", _fast, "task b")

        for _ in range(50):
            tasks = runner.list_all()
            if all(t["status"] in ("completed", "failed") for t in tasks):
                break
            time.sleep(0.01)

        tasks = runner.list_all()
        assert len(tasks) == 2
        assert tasks[0]["task_id"] == "a"  # sorted by started_at
        assert tasks[1]["task_id"] == "b"

    def test_summary_text(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)

        def _ok() -> tuple:
            return "done", {}
        runner.launch("task-x", _ok, "description of x")
        for _ in range(50):
            if runner.get_status("task-x") == "completed":
                break
            time.sleep(0.01)

        summary = runner.summary_text()
        assert "[Background Tasks]" in summary
        assert "task-x" in summary
        assert "completed" in summary

    def test_summary_text_empty(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)
        assert runner.summary_text() == ""

    def test_summary_text_includes_running(self, tmp_path: Path):
        import threading
        barrier = threading.Event()

        def _slow() -> tuple:
            barrier.wait()
            return "x", {}

        runner.launch("running-one", _slow, "analyzing auth module")
        summary = runner.summary_text()
        assert "running" in summary
        assert "running-one" in summary
        assert "analyzing auth module" in summary
        barrier.set()
        for _ in range(50):
            if runner.get_status("running-one") == "completed":
                break
            time.sleep(0.01)

    def test_clear_completed(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)

        def _ok() -> tuple:
            return "done", {}
        runner.launch("keep-me", _ok, "")
        for _ in range(50):
            if runner.get_status("keep-me") == "completed":
                break
            time.sleep(0.01)

        removed = runner.clear_completed()
        assert removed == 1
        assert runner.get_status("keep-me") == "completed"  # file still exists
        # But memory tracking is gone → list_all won't include it
        tasks = runner.list_all()
        assert len(tasks) == 0

    def test_result_persisted_to_disk(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)

        def _work() -> tuple:
            return "disk result", {"Bash": 1}

        runner.launch("disk-task", _work, "disk")
        for _ in range(50):
            if runner.get_status("disk-task") == "completed":
                break
            time.sleep(0.01)

        # Verify file on disk
        result_path = tmp_path / ".tasks" / "output" / "disk-task.json"
        assert result_path.exists()
        data = json.loads(result_path.read_text(encoding="utf-8"))
        assert data["status"] == "completed"
        assert data["result"] == "disk result"

    def test_duplicate_task_id_noop(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)

        def _work() -> tuple:
            return "first", {}
        runner.launch("dup-id", _work, "")
        runner.launch("dup-id", _work, "")  # should no-op

        tasks = runner.list_all()
        assert len(tasks) == 1

    def test_failed_task_error_persisted(self, tmp_path: Path):
        runner = BackgroundTaskRunner(tmp_path)

        def _fail() -> tuple:
            raise ValueError("something went wrong")

        runner.launch("error-task", _fail, "")
        for _ in range(50):
            if runner.get_status("error-task") == "failed":
                break
            time.sleep(0.01)

        result = runner.get_result("error-task")
        assert result is not None
        assert "something went wrong" in result.get("result", "")


class TestPlanModeTools:
    """Integration-light tests for EnterPlanMode / ExitPlanMode tools."""

    def test_enter_plan_mode_tool(self, tmp_path: Path):
        from tools.builtin.enter_plan_mode import EnterPlanModeTool

        agent = Mock()
        agent.enter_plan_mode = Mock()
        tool = EnterPlanModeTool(project_root=tmp_path, code_agent=agent)

        response = json.loads(tool.run({}))
        assert response["status"] == "success"
        assert response["data"]["mode"] == "plan"
        agent.enter_plan_mode.assert_called_once()

    def test_exit_plan_mode_tool(self, tmp_path: Path):
        from tools.builtin.exit_plan_mode import ExitPlanModeTool

        agent = Mock()
        agent.exit_plan_mode = Mock()
        tool = ExitPlanModeTool(project_root=tmp_path, code_agent=agent)

        plan = "1. Refactor auth\n2. Add tests\n3. Update docs"
        response = json.loads(tool.run({"plan": plan}))
        assert response["status"] == "success"
        agent.exit_plan_mode.assert_called_once_with(plan=plan)

    def test_exit_plan_mode_missing_plan(self, tmp_path: Path):
        from tools.builtin.exit_plan_mode import ExitPlanModeTool

        agent = Mock()
        tool = ExitPlanModeTool(project_root=tmp_path, code_agent=agent)

        response = json.loads(tool.run({}))
        assert response["status"] == "error"
        assert "plan" in response["text"].lower()

    def test_exit_plan_mode_empty_plan(self, tmp_path: Path):
        from tools.builtin.exit_plan_mode import ExitPlanModeTool

        agent = Mock()
        tool = ExitPlanModeTool(project_root=tmp_path, code_agent=agent)

        response = json.loads(tool.run({"plan": ""}))
        assert response["status"] == "error"


class TestTaskOutputTool:
    """Tests for TaskOutput tool protocol."""

    def test_task_output_success(self, tmp_path: Path):
        from tools.builtin.task_output import TaskOutputTool
        from core.background_task import BackgroundTaskRunner

        runner = BackgroundTaskRunner(tmp_path)
        tool = TaskOutputTool(project_root=tmp_path, background_runner=runner)

        def _work() -> tuple:
            return "output result", {"Read": 1}

        runner.launch("bg-1", _work, "")
        for _ in range(50):
            if runner.get_status("bg-1") == "completed":
                break
            time.sleep(0.01)

        response = json.loads(tool.run({"task_id": "bg-1"}))
        assert response["status"] == "success"
        assert "output result" in response["data"]["result"]

    def test_task_output_not_found(self, tmp_path: Path):
        from tools.builtin.task_output import TaskOutputTool
        from core.background_task import BackgroundTaskRunner

        runner = BackgroundTaskRunner(tmp_path)
        tool = TaskOutputTool(project_root=tmp_path, background_runner=runner)

        response = json.loads(tool.run({"task_id": "no-such-task"}))
        assert response["status"] == "error"

    def test_task_output_running(self, tmp_path: Path):
        import threading
        from tools.builtin.task_output import TaskOutputTool
        from core.background_task import BackgroundTaskRunner

        runner = BackgroundTaskRunner(tmp_path)
        tool = TaskOutputTool(project_root=tmp_path, background_runner=runner)
        barrier = threading.Event()

        def _slow() -> tuple:
            barrier.wait()
            return "x", {}

        runner.launch("slow-bg", _slow, "")
        response = json.loads(tool.run({"task_id": "slow-bg"}))
        assert response["status"] == "success"
        assert response["data"]["status"] == "running"
        barrier.set()
        for _ in range(50):
            if runner.get_status("slow-bg") == "completed":
                break
            time.sleep(0.01)

    def test_task_output_missing_id(self, tmp_path: Path):
        from tools.builtin.task_output import TaskOutputTool
        from core.background_task import BackgroundTaskRunner

        runner = BackgroundTaskRunner(tmp_path)
        tool = TaskOutputTool(project_root=tmp_path, background_runner=runner)

        response = json.loads(tool.run({}))
        assert response["status"] == "error"


class TestPlanModeFiltering:
    """Tests for plan mode tool filtering in CodeAgent."""

    def test_plan_mode_tools_are_read_only(self):
        from agents.codeAgent import CodeAgent
        plan_tools = CodeAgent.PLAN_MODE_TOOLS
        # Must include read-only tools
        assert "Read" in plan_tools
        assert "Grep" in plan_tools
        assert "Glob" in plan_tools
        assert "LS" in plan_tools
        # Must NOT include write tools
        assert "Write" not in plan_tools
        assert "Edit" not in plan_tools
        assert "Bash" not in plan_tools
        assert "Task" not in plan_tools
        # Must include plan mode control tools
        assert "EnterPlanMode" in plan_tools
        assert "ExitPlanMode" in plan_tools
