"""Subagent event bridge — translate subagent progress into AgentEvents.

Subagents run either synchronously inside a Task tool call or asynchronously
inside BackgroundTaskRunner daemon threads. This bridge gives both paths a
small, shared, thread-safe-ish adapter: it carries no mutable state beyond
constructor fields and simply forwards events to the injected EventSink.

EventSink implementations used by the runtime today are thread-safe:
- EventSink default: no-op
- SessionController's _QueueEventSink: queue.Queue.put()
Custom sinks should keep ``emit()`` thread-safe because background subagents
may call it from daemon threads.
"""

from __future__ import annotations

from typing import Any

from core.events import AgentEvent, EventSink, EventType


class SubagentEventBridge:
    """Translate subagent lifecycle / streaming / tool progress to AgentEvents."""

    def __init__(
        self,
        sink: EventSink | None,
        subagent_id: str,
        parent_step: int = 0,
    ) -> None:
        self._sink = sink or EventSink()
        self.subagent_id = subagent_id
        self.parent_step = parent_step

    def emit_started(self, *, description: str = "", prompt_preview: str = "") -> None:
        self._emit(
            EventType.SUBAGENT_STARTED,
            {
                "description": description,
                "prompt_preview": prompt_preview,
            },
        )

    def emit_llm_delta(self, kind: str, text: str, *, subagent_step: int | None = None) -> None:
        """Emit one streaming delta.

        ``kind`` mirrors HelloAgentsLLM.stream_raw's event types (currently
        "content" / "reasoning", and future-safe for provider additions).
        """
        payload: dict[str, Any] = {"kind": kind, "text": text}
        if subagent_step is not None:
            payload["subagent_step"] = subagent_step
        self._emit(EventType.SUBAGENT_DELTA, payload)

    def emit_tool(
        self,
        *,
        phase: str,
        name: str,
        args: dict[str, Any] | None = None,
        result_preview: str = "",
        subagent_step: int | None = None,
    ) -> None:
        """Emit a subagent tool event.

        ``phase`` is usually "started" or "completed"; using one event type
        with a phase field keeps the Web/TUI reducers simple while retaining
        parity with main-agent tool.started/tool.completed.
        """
        payload: dict[str, Any] = {
            "phase": phase,
            "name": name,
            "args": args or {},
            "result_preview": result_preview,
        }
        if subagent_step is not None:
            payload["subagent_step"] = subagent_step
        self._emit(EventType.SUBAGENT_TOOL_USE, payload)

    def emit_finished(
        self,
        *,
        ok: bool,
        result_preview: str = "",
        steps: int = 0,
    ) -> None:
        self._emit(
            EventType.SUBAGENT_FINISHED,
            {
                "ok": ok,
                "result_preview": result_preview,
                "steps": steps,
            },
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self._sink.emit(
                AgentEvent(
                    event_type,
                    payload,
                    step=self.parent_step,
                    subagent_id=self.subagent_id,
                )
            )
        except Exception:
            # Event reporting must never break subagent execution. A broken UI
            # sink should degrade visibility, not correctness.
            pass
