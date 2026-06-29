"""Structured agent events — decouple Agent core from UI rendering.

Every meaningful agent lifecycle transition emits an ``AgentEvent``.
UI layers (CLI/TUI, Web, desktop) subscribe via an ``EventSink`` instead
of overriding ``_console()`` or monkey-patching internal methods.

Usage in CodeAgent::

    self.event_sink.emit(AgentEvent("tool.started", {"name": "Read", ...}))

Usage in UI::

    class MySink(EventSink):
        def emit(self, event):
            if event.type == "tool.started":
                print(f"Tool: {event.payload['name']}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ── Event type literals ──────────────────────────────────────────────
# Using plain strings rather than Literal to avoid typing_extensions
# dependency and keep the module importable anywhere.

class EventType:
    """Well-known event type strings (namespace, not enum — duck-type friendly)."""
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    STEP_STARTED = "step.started"
    LLM_STARTED = "llm.started"
    LLM_COMPLETED = "llm.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    ASSISTANT_FINAL = "assistant.final"
    # Session-layer events (emitted by SessionController, not CodeAgent):
    PERMISSION_REQUESTED = "permission.requested"
    ASK_USER_REQUESTED = "ask_user.requested"
    TURN_COMPLETED = "turn.completed"

    # Subagent events (emitted by Task/BackgroundTask/Team workers):
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_DELTA = "subagent.delta"
    SUBAGENT_TOOL_USE = "subagent.tool_use"
    SUBAGENT_FINISHED = "subagent.finished"


# ── Event dataclass ──────────────────────────────────────────────────

@dataclass
class AgentEvent:
    """A lightweight structured event emitted during agent execution.

    No Pydantic dependency — plain dataclass with a dict payload.
    UI adapters consume these to render tool timelines, status bars, etc.
    """
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    step: int = 0
    subagent_id: Optional[str] = None


# ── Sink interface ───────────────────────────────────────────────────

class EventSink:
    """Receives agent events.  Override in UI adapters.

    The default implementation is a no-op — suitable for headless /
    testing / non-interactive use.
    """

    def emit(self, event: AgentEvent) -> None:
        """Handle a single event (no-op by default)."""
        pass
