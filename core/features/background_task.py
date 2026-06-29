from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent

logger = logging.getLogger(__name__)


class BackgroundTaskFeature(AgentFeature):
    """Manages background (daemon-thread) sub-agent tasks.

    The main loop continues while background tasks run in parallel.
    Results are retrieved via TaskOutput.
    """

    name = "background_task"
    order = 70

    def init(self, agent: "CodeAgent") -> None:
        from core.background_task import BackgroundTaskRunner

        agent._background_runner = BackgroundTaskRunner(
            project_root=agent._original_project_root
        )

        # Register the agent's event sink as a progress observer so that
        # background subagent execution is visible in real-time via the
        # same event bus used by the main agent.
        self._fanout_handler = self._make_fanout(agent)
        if self._fanout_handler is not None:
            agent._background_runner.register_observer(self._fanout_handler)

    @staticmethod
    def _make_fanout(agent: "CodeAgent") -> object | None:
        sink = getattr(agent, "event_sink", None)
        if sink is None:
            return None

        def _on_progress(task_id: str, record: dict) -> None:
            from core.events import AgentEvent, EventType
            from contextlib import suppress

            event_type = {
                "action": EventType.SUBAGENT_TOOL_USE,
                "thought": EventType.SUBAGENT_DELTA,
                "error": EventType.SUBAGENT_DELTA,
            }.get(str(record.get("type") or ""), EventType.SUBAGENT_DELTA)

            payload: dict = {"kind": str(record.get("type", "")), "subagent_step": record.get("step", 0)}
            # Enrich payload with whatever the progress callback provides.
            if record.get("content"):
                payload["text"] = str(record["content"])
            if record.get("tool"):
                payload["name"] = str(record["tool"])
                payload["phase"] = "unknown"
            if record.get("message"):
                payload["text"] = str(record["message"])

            with suppress(Exception):
                sink.emit(
                    AgentEvent(event_type, payload, step=0, subagent_id=task_id)
                )

        return _on_progress

    def cleanup(self, agent: "CodeAgent") -> None:
        if self._fanout_handler is not None:
            try:
                agent._background_runner.unregister_observer(self._fanout_handler)
            except Exception:
                pass

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        summary = agent._background_runner.summary_text()
        return [summary] if summary else []
