"""Workflow orchestration: bounded fan-out, pipelining and the Workflow tool."""
from __future__ import annotations

import json
import threading
import time

import pytest

from core.workflow import TaskSpec, Workflow, WorkflowError
from tools.builtin.workflow import WorkflowTool


class Recorder:
    def __init__(self, delay: float = 0.0, fail_on: set[str] | None = None):
        self.delay = delay
        self.fail_on = fail_on or set()
        self.prompts: list[tuple[str, str]] = []
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, prompt: str, agent_type: str) -> str:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.prompts.append((prompt, agent_type))
        try:
            if self.delay:
                time.sleep(self.delay)
            if prompt in self.fail_on:
                raise RuntimeError(f"boom: {prompt}")
            return f"result:{prompt}"
        finally:
            with self._lock:
                self.active -= 1


# ---------------------------------------------------------------------------
# Core workflows
# ---------------------------------------------------------------------------
def test_parallel_runs_every_task_and_keeps_order():
    recorder = Recorder()
    result = Workflow(recorder).run_parallel([
        TaskSpec("a", "one"),
        TaskSpec("b", "two", agent_type="explore"),
    ])
    assert [o.id for o in result.outcomes] == ["a", "b"]
    assert [o.result for o in result.outcomes] == ["result:one", "result:two"]
    assert result.completed == 2
    assert ("two", "explore") in recorder.prompts


def test_parallel_respects_the_concurrency_cap():
    recorder = Recorder(delay=0.05)
    tasks = [TaskSpec(str(i), f"p{i}") for i in range(6)]
    Workflow(recorder, max_concurrency=2).run_parallel(tasks)
    assert recorder.peak <= 2


def test_one_failure_does_not_cancel_the_others():
    recorder = Recorder(fail_on={"bad"})
    result = Workflow(recorder).run_parallel([
        TaskSpec("a", "good"),
        TaskSpec("b", "bad"),
        TaskSpec("c", "also good"),
    ])
    statuses = {o.id: o.status for o in result.outcomes}
    assert statuses == {"a": "completed", "b": "failed", "c": "completed"}
    assert "boom" in result.outcomes[1].error
    assert result.completed == 2


def test_invalid_definitions_are_rejected():
    workflow = Workflow(Recorder())
    with pytest.raises(WorkflowError):
        workflow.run_parallel([])
    with pytest.raises(WorkflowError):
        workflow.run_parallel([TaskSpec("a", "x"), TaskSpec("a", "y")])
    with pytest.raises(WorkflowError):
        workflow.run_parallel([TaskSpec("", "x")])


def test_pipeline_interpolates_item_and_prev():
    recorder = Recorder()
    result = Workflow(recorder).run_pipeline(
        ["file1", "file2"],
        [TaskSpec("scan", "scan {item}"), TaskSpec("summarize", "summarize {item} from {prev}")],
    )
    assert result.completed == 2
    assert [o.result for o in result.outcomes] == [
        "result:summarize file1 from result:scan file1",
        "result:summarize file2 from result:scan file2",
    ]


def test_pipeline_failure_skips_the_rest_of_that_item_only():
    recorder = Recorder(fail_on={"scan file2"})
    result = Workflow(recorder).run_pipeline(
        ["file1", "file2"],
        [TaskSpec("scan", "scan {item}"), TaskSpec("summarize", "summarize {item} from {prev}")],
    )
    assert result.outcomes[0].status == "completed"
    assert result.outcomes[1].status == "failed"
    assert "boom" in result.outcomes[1].error
    # The failing item never reached the second stage.
    prompts = [prompt for prompt, _ in recorder.prompts]
    assert "summarize file2 from result:scan file2" not in prompts


def test_unknown_placeholder_is_rejected():
    with pytest.raises(WorkflowError):
        Workflow(Recorder()).run_pipeline(["x"], [TaskSpec("s", "do {nope}")])


# ---------------------------------------------------------------------------
# Tool contract
# ---------------------------------------------------------------------------
def test_tool_parallel_mode(tmp_path):
    recorder = Recorder()
    tool = WorkflowTool(project_root=str(tmp_path), runner=recorder)
    payload = json.loads(tool.run({
        "mode": "parallel",
        "tasks": [{"id": "a", "prompt": "one"}, {"id": "b", "prompt": "two"}],
        "max_concurrency": 2,
    }))
    assert payload["status"] == "success"
    assert payload["data"]["completed"] == 2
    assert "2/2 completed" in payload["text"]


def test_tool_pipeline_mode(tmp_path):
    recorder = Recorder()
    tool = WorkflowTool(project_root=str(tmp_path), runner=recorder)
    payload = json.loads(tool.run({
        "mode": "pipeline",
        "items": ["a", "b"],
        "stages": [{"id": "s1", "prompt": "look at {item}"}],
    }))
    assert payload["status"] == "success"
    assert payload["data"]["total"] == 2


def test_tool_rejects_bad_input(tmp_path):
    tool = WorkflowTool(project_root=str(tmp_path), runner=Recorder())
    payload = json.loads(tool.run({"mode": "sideways"}))
    assert payload["error"]["code"] == "INVALID_PARAM"

    payload = json.loads(tool.run({"mode": "parallel", "tasks": []}))
    assert payload["error"]["code"] == "INVALID_PARAM"

    payload = json.loads(tool.run({
        "mode": "parallel", "tasks": [{"id": "a", "prompt": ""}],
    }))
    assert payload["error"]["code"] == "INVALID_PARAM"


def test_tool_without_subagent_support_reports_cleanly(tmp_path):
    tool = WorkflowTool(project_root=str(tmp_path), code_agent=object())
    payload = json.loads(tool.run({
        "mode": "parallel", "tasks": [{"id": "a", "prompt": "x"}],
    }))
    assert payload["status"] == "success"  # per-task failure, not a tool error
    assert payload["data"]["outcomes"][0]["status"] == "failed"
    assert "subagent" in payload["data"]["outcomes"][0]["error"].lower()
