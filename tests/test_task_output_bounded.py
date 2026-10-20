"""TaskOutput must keep one background task result bounded and well-formed."""
from __future__ import annotations

import json
from unittest.mock import Mock

from tools.builtin.task_output import TaskOutputTool


def tool(tmp_path):
    runner = Mock()
    t = TaskOutputTool(project_root=tmp_path, background_runner=runner)
    return t, runner


def test_huge_completed_background_result_is_bounded(tmp_path):
    t, runner = tool(tmp_path)
    runner.get_status.return_value = "completed"
    runner.get_rich_status.return_value = {}
    runner.get_result.return_value = {
        "status": "completed",
        "result": "A" * 500000,
        "tool_usage": {"Bash": 2, "Read": 4},
    }
    out = json.loads(t.run({"task_id": "task-1"}))
    assert out["status"] == "success"
    assert out["data"]["result_truncated"] is True
    assert len(out["data"]["result"]) == t.MAX_RESULT_CHARS
    assert len(out["text"]) < t.MAX_RESULT_CHARS + 200
    assert out["stats"]["tool_calls"] == 6
    assert len(json.dumps(out)) < t.MAX_RESULT_CHARS * 2 + 5000


def test_running_event_is_bounded_and_noncontent_keys_ignored(tmp_path):
    t, runner = tool(tmp_path)
    runner.get_status.return_value = "running"
    runner.get_rich_status.return_value = {
        "elapsed": 3.5,
        "last_event": {
            "type": "tool",
            "content": "Q" * 200000,
            "private_raw": "SECRET_NEVER_SERIALIZED",
        },
    }
    out = json.loads(t.run({"task_id": "task-2"}))
    assert out["status"] == "success"
    assert len(out["data"]["last_event"]["content"]) == t.MAX_EVENT_CHARS
    assert "SECRET_NEVER_SERIALIZED" not in json.dumps(out)


def test_corrupt_result_usage_does_not_break_tool_contract(tmp_path):
    t, runner = tool(tmp_path)
    runner.get_status.return_value = "completed"
    runner.get_rich_status.return_value = None
    runner.get_result.return_value = {
        "status": "failed", "result": None,
        "tool_usage": {"Read": "invalid", "Bash": 3, "Grep": -5},
    }
    out = json.loads(t.run({"task_id": "task-3"}))
    assert out["status"] == "success"
    assert out["stats"]["tool_calls"] == 3
