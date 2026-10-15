"""Framework-owned VCRService; not a legacy Feature or a plugin extension."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class VCRService:
    """Intercepts LLM calls to replay cached fixtures or record new ones.

    Only active when ``VCR_ENABLED=true``.
    """

    name = "vcr"
    order = 90

    def start(self, agent: "CodeAgent") -> None:
        from core.vcr import VCR

        agent._vcr = VCR.from_env()

    def llm_intercept(
        self,
        agent: "CodeAgent",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str,
        fallback: Any,
    ) -> Any:
        if not agent._vcr.enabled:
            return fallback()
        return agent._vcr.call(
            model=agent.llm.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            fallback=fallback,
        )
