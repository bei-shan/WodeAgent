"""OutputStyleFeature — response style switching (default/explanatory/learning)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class OutputStyleFeature(AgentFeature):
    """Injects output-style prompts into the L1 system message.

    Uses the ``{output_style}`` placeholder in L1_system_prompt.py.
    """

    name = "output_style"
    order = 80

    def init(self, agent: "CodeAgent") -> None:
        from core.output_styles import OutputStyleManager

        env_style = os.getenv("AGENT_OUTPUT_STYLE", "").strip()
        agent._output_style_manager = OutputStyleManager(
            project_root=agent._original_project_root,
            env_style=env_style if env_style else None,
        )

    def post_init(self, agent: "CodeAgent") -> None:
        """Push style prompt into context builder (needs context_builder ready)."""
        prompt = agent._output_style_manager.get_current_prompt()
        agent.context_builder.set_output_style_prompt(prompt)
