"""TUI subagent renderer — compact single-line progress for daemon subagents.

This is a small EventSink that prints subagent activity as one-liners
below the main agent panel. It buffers events while Rich Live is active
and flushes when the Live context exits, which avoids the known Rich
concurrent-printing anti-pattern.

Design constraint (Phase 5 NG2): we intentionally do NOT attempt
concurrent Rich Live panels. This renderer provides visibility without
breaking the existing single-Live TUI model. See the Phase 5 design doc
for alternatives and the known-compromise documentation.

Usage in chat_test_agent.py::

    from tui.subagent_renderer import TuiSubagentSink
    agent.event_sink = TuiSubagentSink(wrapped=agent.event_sink)
"""

from __future__ import annotations

from core.events import AgentEvent, EventSink, EventType


class TuiSubagentSink(EventSink):
    """Thin EventSink wrapper that prints subagent events as one-liners.

    All non-subagent events pass through to the wrapped sink unchanged.
    Subagent events are buffered in memory while the main-agent Rich Live
    is active, then flushed when ``flush_buffer()`` is called.
    """

    def __init__(self, wrapped: EventSink | None = None) -> None:
        self._wrapped = wrapped or EventSink()
        self._buffer: list[str] = []
        self._live_active = True

    def emit(self, event: AgentEvent) -> None:
        # Always pass through to the wrapped sink for Web/SessionController.
        self._wrapped.emit(event)

        # Strip subagent events into compact one-liners.
        if event.subagent_id is None:
            return

        line: str | None = None
        sid = event.subagent_id[:12]
        if event.type == EventType.SUBAGENT_STARTED:
            desc = str(event.payload.get("description", ""))[:60]
            line = f"[{sid}] ▶ started — {desc}"
        elif event.type == EventType.SUBAGENT_DELTA:
            text = str(event.payload.get("text", ""))[:80]
            if text.strip():
                line = f"[{sid}] … {text}"
        elif event.type == EventType.SUBAGENT_TOOL_USE:
            phase = event.payload.get("phase", "?")
            name = event.payload.get("name", "?")
            line = f"[{sid}] ⚙ {phase} {name}"
        elif event.type == EventType.SUBAGENT_FINISHED:
            ok = "✅" if event.payload.get("ok") else "❌"
            line = f"[{sid}] {ok} finished (steps={event.payload.get('steps', '?')})"

        if line is None:
            return

        if self._live_active:
            self._buffer.append(line)
        else:
            print(line)

    def set_live_active(self, active: bool) -> None:
        self._live_active = active

    def flush_buffer(self) -> None:
        for line in self._buffer:
            print(line)
        self._buffer.clear()
