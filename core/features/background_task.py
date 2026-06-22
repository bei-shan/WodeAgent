"""BackgroundTaskFeature — daemon-thread sub-agent execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


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

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        summary = agent._background_runner.summary_text()
        return [summary] if summary else []
