"""Step 28 — Web/TUI rendering integration tests.

Covers:
- WebSocket _STREAM_EVENTS includes subagent event types
- TuiSubagentSink buffers during Live, flushes after
- TuiSubagentSink passes through to wrapped sink

Run:
    python -m pytest tests/test_subagent_streaming_step28.py -v
"""

from __future__ import annotations

from core.events import AgentEvent, EventSink, EventType
from tui.subagent_renderer import TuiSubagentSink


# ---------------------------------------------------------------------------
# _STREAM_EVENTS check
# ---------------------------------------------------------------------------


def test_stream_events_includes_subagent_types():
    """Verify the WebSocket whitelist includes subagent events."""
    # Re-read the set to confirm our edit took effect.
    from desktop.service.app import _STREAM_EVENTS

    assert EventType.SUBAGENT_STARTED in _STREAM_EVENTS
    assert EventType.SUBAGENT_DELTA in _STREAM_EVENTS
    assert EventType.SUBAGENT_TOOL_USE in _STREAM_EVENTS
    assert EventType.SUBAGENT_FINISHED in _STREAM_EVENTS


# ---------------------------------------------------------------------------
# TuiSubagentSink
# ---------------------------------------------------------------------------


class ListSink(EventSink):
    """Simple recording sink."""
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []
    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


def test_tui_sink_passes_through_to_wrapped():
    """Non-subagent events and subagent events both reach the wrapped sink."""
    inner = ListSink()
    sink = TuiSubagentSink(wrapped=inner)

    main_event = AgentEvent(EventType.TOOL_STARTED, {"name": "Read"}, step=1)
    sub_event = AgentEvent(
        EventType.SUBAGENT_STARTED,
        {"description": "test sub"},
        step=1,
        subagent_id="task-deadbeef",
    )

    sink.emit(main_event)
    sink.emit(sub_event)

    # Both events must be in the inner sink (wrapped is always called).
    assert len(inner.events) == 2
    assert inner.events[0].type == EventType.TOOL_STARTED
    assert inner.events[1].type == EventType.SUBAGENT_STARTED


def test_tui_sink_buffers_during_live():
    """During live activity, subagent lines are buffered not printed."""
    sink = TuiSubagentSink()

    sub_event = AgentEvent(
        EventType.SUBAGENT_DELTA,
        {"kind": "content", "text": "hello"},
        step=1,
        subagent_id="task-buffer",
    )

    # Live active → buffer, no crash
    sink.set_live_active(True)
    sink.emit(sub_event)

    assert len(sink._buffer) == 1
    assert "hello" in sink._buffer[0]


def test_tui_sink_flushes_buffer():
    """flush_buffer clears the internal buffer."""
    sink = TuiSubagentSink()
    sink._buffer = ["line1", "line2"]
    sink.flush_buffer()
    assert sink._buffer == []


def test_tui_sink_skips_non_subagent_events():
    """Events without subagent_id are forwarded but produce no buffer line."""
    inner = ListSink()
    sink = TuiSubagentSink(wrapped=inner)

    sink.emit(AgentEvent(EventType.TOOL_STARTED, {}, step=1))
    assert len(sink._buffer) == 0
    assert len(inner.events) == 1  # forwarded
