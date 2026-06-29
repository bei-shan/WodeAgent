"""Step 26 — sync Task path wiring integration tests.

Verifies that SubagentRunner with event_sink emits the full lifecycle:
started → delta(s) → tool use → ... → finished.

Run:
    python -m pytest tests/test_subagent_streaming_step26.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

import pytest

from tools.builtin.task import SubagentRunner
from tests.test_subagent_streaming_step25 import RecordingSink


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_that_replies(content: str = "answer"):
    llm = Mock()
    llm.invoke_raw.return_value = {
        "choices": [{"message": {"content": content}}],
    }
    return llm


def _make_registry_with_tool(name: str = "Read"):
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    mock = Mock()
    mock.name = name
    mock.description = f"{name} tool"
    mock.get_parameters.return_value = []
    mock.run.return_value = '{"status":"success","data":{},"text":"ok"}'
    registry.register_tool(mock)
    return registry


# ---------------------------------------------------------------------------
# No event_sink → zero impact on existing callers
# ---------------------------------------------------------------------------


def test_runner_without_event_sink_runs_normally(tmp_path):
    """SubagentRunner without event_sink still completes (zero regression)."""
    runner = SubagentRunner(
        llm=_make_llm_that_replies("done"),
        tool_registry=_make_registry_with_tool(),
        system_prompt="You are a test agent",
        project_root=tmp_path,
        max_steps=5,
    )
    result, _ = runner.run("Read something")
    assert "done" in result


def test_runner_without_event_sink_no_bridge_emission():
    """With no event_sink, SubagentRunner creates no event_bridge, no events
    are sent. Verifies this is a clean zero-impact path."""
    runner = SubagentRunner(
        llm=Mock(),
        tool_registry=Mock(),
        system_prompt=".",
        project_root=Mock(),
        max_steps=5,
        event_sink=None,
    )
    assert runner.event_bridge is None  # no bridge when sink is None


# ---------------------------------------------------------------------------
# With event_sink → full lifecycle emitted
# ---------------------------------------------------------------------------


def test_sync_task_emits_lifecycle_events(tmp_path):
    """Sync subagent run emits: started → delta+tool → finished."""
    sink = RecordingSink()
    llm = _make_llm_that_replies("Final answer: all good")
    # Add stream_raw so TurnExecutor takes the streaming path.
    llm.stream_raw = Mock()

    def fake_stream(messages, tools=None, tool_choice="auto", on_delta=None):
        if on_delta:
            on_delta("content", "Final")
            on_delta("content", " answer: all good")
        return llm.invoke_raw.return_value

    llm.stream_raw.side_effect = fake_stream

    runner = SubagentRunner(
        llm=llm,
        tool_registry=_make_registry_with_tool(),
        system_prompt="You are a test agent",
        project_root=tmp_path,
        max_steps=5,
        event_sink=sink,
        subagent_id="task-step26-test",
        parent_step=3,
    )
    result, _ = runner.run("Test task prompt")
    assert "all good" in result

    # Check event sequence: started → delta(s) → finished
    types = [e.type for e in sink.events]
    assert "subagent.started" in types
    # Deltas present
    deltas = [e for e in sink.events if e.type == "subagent.delta"]
    assert len(deltas) >= 1
    # Finished at the end
    assert types[-1] == "subagent.finished"
    last = sink.events[-1]
    assert last.payload["ok"] is True
    assert "all good" in last.payload["result_preview"]

    # All events share the same subagent_id and parent_step
    assert all(e.subagent_id == "task-step26-test" for e in sink.events)
    assert all(e.step == 3 for e in sink.events)


def test_sync_task_with_tool_calls_emits_tool_events(tmp_path):
    """A subagent that calls a tool should emit subagent.tool_use events."""
    sink = RecordingSink()

    # Two LLM responses: first with tool call, second final.
    def make_raw(content="", tool_calls=None):
        msg = {"content": content}
        if tool_calls is not None:
            msg["tool_calls"] = tool_calls
        return {"choices": [{"message": msg}]}

    llm = Mock()
    llm.stream_raw = Mock()
    llm.stream_raw.side_effect = [
        make_raw(tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": "Read", "arguments": '{"file_path":"test.txt"}'},
        }]),
        make_raw(content="Final: ok"),
    ]
    llm.invoke_raw.return_value = make_raw(content="Final: ok")

    runner = SubagentRunner(
        llm=llm,
        tool_registry=_make_registry_with_tool("Read"),
        system_prompt="You are a test agent",
        project_root=tmp_path,
        max_steps=5,
        event_sink=sink,
        subagent_id="task-step26-tool",
        parent_step=1,
    )
    runner.run("Read test.txt")

    tool_events = [e for e in sink.events if e.type == "subagent.tool_use"]
    assert len(tool_events) >= 2  # started + completed
    phases = [e.payload["phase"] for e in tool_events]
    assert "started" in phases
    assert "completed" in phases
    assert tool_events[0].payload["name"] == "Read"


def test_sync_task_error_emits_failed_finished(tmp_path):
    """LLM error during run → subagent.finished with ok=False."""
    sink = RecordingSink()
    llm = Mock()
    llm.stream_raw = Mock()
    llm.stream_raw.side_effect = RuntimeError("LLM API error")
    llm.invoke_raw.side_effect = RuntimeError("LLM API error")  # both paths fail

    runner = SubagentRunner(
        llm=llm,
        tool_registry=_make_registry_with_tool(),
        system_prompt="You are a test agent",
        project_root=tmp_path,
        max_steps=5,
        event_sink=sink,
        subagent_id="task-step26-err",
        parent_step=1,
    )
    runner.run("Doomed task")

    assert sink.events[-1].type == "subagent.finished"
    assert sink.events[-1].payload["ok"] is False
