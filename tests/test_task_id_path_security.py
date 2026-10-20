"""Background Task IDs are file basenames, never untrusted filesystem paths."""
from __future__ import annotations

import json

import pytest

from core.background_task import BackgroundTaskRunner, valid_task_id
from tools.builtin.task_output import TaskOutputTool


@pytest.mark.parametrize("bad", [
    "../private", "../../secret", "..", ".", "/etc/passwd", "a/b",
    "a\\b", "x\x00y", "", " " * 3, ".hidden", "x" * 130,
])
def test_task_storage_rejects_unsafe_file_ids(tmp_path, bad):
    runner = BackgroundTaskRunner(project_root=tmp_path)
    assert not valid_task_id(bad)
    with pytest.raises(ValueError, match="safe opaque"):
        runner.launch(bad, lambda: ("executed", {}))
    assert runner.get_result(bad) is None
    assert runner.get_progress(bad) == []
    with pytest.raises(ValueError, match="unsafe"):
        runner._save_result(bad, "completed", "dangerous", {})


def test_task_output_rejects_traversal_before_looking_up_real_file(tmp_path):
    runner = BackgroundTaskRunner(project_root=tmp_path)
    private = tmp_path / ".tasks" / "private.json"
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text(json.dumps({"result": "VERY_SECRET"}))
    assert private.exists()
    tool = TaskOutputTool(project_root=tmp_path, background_runner=runner)
    result = json.loads(tool.run({"task_id": "../private"}))
    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_PARAM"
    assert "VERY_SECRET" not in json.dumps(result)


def test_valid_existing_task_results_work(tmp_path):
    runner = BackgroundTaskRunner(project_root=tmp_path)
    runner._save_result("bg_012345abcd", "completed", "safe answer", {"Read": 2})
    tool = TaskOutputTool(project_root=tmp_path, background_runner=runner)
    result = json.loads(tool.run({"task_id": "bg_012345abcd"}))
    assert result["status"] == "success"
    assert result["data"]["result"] == "safe answer"
