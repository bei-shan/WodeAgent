"""Subagent streaming primitives — Step 25 tests.

Covers:
- AgentEvent.subagent_id field and new subagent EventType constants
- SubagentEventBridge event emission
- TurnExecutor.execute_turn(on_delta=...) streaming path
- TurnExecutor fallback to invoke_raw if stream_raw fails

Run:
    python -m pytest tests/test_subagent_streaming_step25.py -v
"""

from __future__ import annotations

from unittest.mock import Mock

from core.events import AgentEvent, EventSink, EventType
from core.runtime.subagent_bridge import SubagentEventBridge
from core.team_engine.turn_executor import TurnExecutor
from tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingSink(EventSink):
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


def _raw(content: str = "", tool_calls=None):
    message = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


# ---------------------------------------------------------------------------
# Event contract
# ---------------------------------------------------------------------------


def test_agent_event_supports_subagent_id():
    event = AgentEvent(
        EventType.SUBAGENT_DELTA,
        {"kind": "content", "text": "hi"},
        step=3,
        subagent_id="task-abc123",
    )
    assert event.type == "subagent.delta"
    assert event.subagent_id == "task-abc123"
    assert event.step == 3


def test_subagent_event_type_constants():
    assert EventType.SUBAGENT_STARTED == "subagent.started"
    assert EventType.SUBAGENT_DELTA == "subagent.delta"
    assert EventType.SUBAGENT_TOOL_USE == "subagent.tool_use"
    assert EventType.SUBAGENT_FINISHED == "subagent.finished"


# ---------------------------------------------------------------------------
# SubagentEventBridge
# ---------------------------------------------------------------------------


def test_subagent_event_bridge_emits_lifecycle_events():
    sink = RecordingSink()
    bridge = SubagentEventBridge(sink, subagent_id="task-1234", parent_step=7)

    bridge.emit_started(description="scan files", prompt_preview="Read src")
    bridge.emit_llm_delta("content", "hello", subagent_step=1)
    bridge.emit_tool(phase="started", name="Read", args={"file_path": "a.py"}, subagent_step=1)
    bridge.emit_tool(phase="completed", name="Read", result_preview="ok", subagent_step=1)
    bridge.emit_finished(ok=True, result_preview="done", steps=2)

    assert [e.type for e in sink.events] == [
        EventType.SUBAGENT_STARTED,
        EventType.SUBAGENT_DELTA,
        EventType.SUBAGENT_TOOL_USE,
        EventType.SUBAGENT_TOOL_USE,
        EventType.SUBAGENT_FINISHED,
    ]
    assert all(e.subagent_id == "task-1234" for e in sink.events)
    assert all(e.step == 7 for e in sink.events)
    assert sink.events[1].payload == {"kind": "content", "text": "hello", "subagent_step": 1}
    assert sink.events[2].payload["phase"] == "started"
    assert sink.events[3].payload["phase"] == "completed"


def test_subagent_event_bridge_swallows_sink_errors():
    sink = Mock(spec=EventSink)
    sink.emit.side_effect = RuntimeError("ui gone")
    bridge = SubagentEventBridge(sink, subagent_id="task-dead", parent_step=1)

    # Visibility failures must never break subagent execution.
    bridge.emit_started(description="x")
    bridge.emit_llm_delta("content", "x")
    bridge.emit_finished(ok=False, result_preview="fail")

    assert sink.emit.call_count == 3


# ---------------------------------------------------------------------------
# TurnExecutor streaming path
# ---------------------------------------------------------------------------


def test_turn_executor_uses_stream_raw_when_on_delta_present(tmp_path):
    llm = Mock()
    deltas: list[tuple[str, str]] = []

    def fake_stream_raw(messages, tools=None, tool_choice="auto", on_delta=None):
        on_delta("content", "Hel")
        on_delta("content", "lo")
        return _raw(content="Final Answer: Hello")

    llm.stream_raw.side_effect = fake_stream_raw
    llm.invoke_raw.return_value = _raw(content="should not be used")

    executor = TurnExecutor(
        llm=llm,
        tool_registry=ToolRegistry(),
        project_root=tmp_path,
    )
    result = executor.execute_turn(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}],
        tool_usage={},
        on_delta=lambda kind, text: deltas.append((kind, text)),
    )

    assert result["done"] is True
    assert "Hello" in result["final_result"]
    assert deltas == [("content", "Hel"), ("content", "lo")]
    llm.stream_raw.assert_called_once()
    llm.invoke_raw.assert_not_called()


def test_turn_executor_falls_back_to_invoke_raw_if_stream_raw_raises(tmp_path):
    llm = Mock()
    llm.stream_raw.side_effect = RuntimeError("provider rejected streaming")
    llm.invoke_raw.return_value = _raw(content="Final Answer: fallback")
    deltas: list[tuple[str, str]] = []

    executor = TurnExecutor(
        llm=llm,
        tool_registry=ToolRegistry(),
        project_root=tmp_path,
    )
    result = executor.execute_turn(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}],
        tool_usage={},
        on_delta=lambda kind, text: deltas.append((kind, text)),
    )

    assert result["done"] is True
    assert "fallback" in result["final_result"]
    assert deltas == []
    llm.stream_raw.assert_called_once()
    llm.invoke_raw.assert_called_once()


def test_turn_executor_keeps_non_streaming_path_when_no_callback(tmp_path):
    llm = Mock()
    llm.invoke_raw.return_value = _raw(content="Final Answer: old path")

    executor = TurnExecutor(
        llm=llm,
        tool_registry=ToolRegistry(),
        project_root=tmp_path,
    )
    result = executor.execute_turn(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}],
        tool_usage={},
    )

    assert "old path" in result["final_result"]
    llm.invoke_raw.assert_called_once()
    llm.stream_raw.assert_not_called()
