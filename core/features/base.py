"""AgentFeature — pluggable feature protocol for CodeAgent.

Each feature represents an independently toggleable capability.
Features interact with the agent through a well-defined lifecycle:
init → post_init → runtime_blocks / pre_tool_use / post_tool_use → cleanup.

Usage::

    class MyFeature(AgentFeature):
        name = "my_feature"
        order = 50

        def init(self, agent):
            agent._my_state = {}

        def runtime_blocks(self, agent, step):
            return ["[MyFeature] active"]
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class AgentFeature(ABC):
    """Pluggable feature module for CodeAgent.

    Subclasses override lifecycle methods as needed.  All methods receive
    the *agent* instance explicitly — features do not hold strong
    references to the agent.

    Attributes
    ----------
    name:
        Unique feature identifier (used for logging and ordering).
    order:
        Initialisation priority.  Lower numbers initialise earlier.
        Default is 50.  Suggested ranges:
        - 10-29: core infrastructure (worktree, MCP)
        - 30-49: team/delegate
        - 50-69: plan, budget, background
        - 70-89: output styles, hooks
        - 90+: VCR, session
    """

    name: str = "base"
    order: int = 50

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self, agent: "CodeAgent") -> None:
        """Called during agent initialisation, before post_init.

        Use this to set attributes on *agent* and initialise state.
        Default: no-op.
        """

    def post_init(self, agent: "CodeAgent") -> None:
        """Called after all features have been init'd and core is ready.

        Use this when you need access to context_builder or other
        features that must already exist.  Default: no-op.
        """

    def cleanup(self, agent: "CodeAgent") -> None:
        """Called during agent.close().  Default: no-op."""

    # ------------------------------------------------------------------
    # Runtime — context injection
    # ------------------------------------------------------------------

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        """Return system-prompt blocks to inject for this ReAct step.

        Called every step.  Return an empty list to inject nothing.
        """
        return []

    # ------------------------------------------------------------------
    # Runtime — tool interception
    # ------------------------------------------------------------------

    def pre_tool_use(
        self, agent: "CodeAgent", tool_name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Intercept tool execution *before* the tool runs.

        Parameters
        ----------
        agent:
            The owning CodeAgent.
        tool_name:
            Name of the tool about to be called.
        tool_input:
            Normalised tool input dict.

        Returns
        -------
        ``None`` to allow the tool to proceed, or a dict with:
        - ``blocked``: bool
        - ``reason``: str (required if blocked)
        - ``updated_input``: dict (shallow-merged into tool_input)
        - ``system_messages``: list[str] (injected into next LLM call)
        """
        return None

    def post_tool_use(
        self, agent: "CodeAgent", tool_name: str, tool_input: dict[str, Any], result: str
    ) -> list[str]:
        """Intercept tool execution *after* the tool runs.

        Returns a list of system messages to inject into the next LLM call.
        """
        return []

    # ------------------------------------------------------------------
    # Runtime — LLM interception
    # ------------------------------------------------------------------

    def llm_intercept(
        self,
        agent: "CodeAgent",
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str,
        fallback: Any,
    ) -> Any:
        """Intercept LLM API calls (e.g. VCR replay).

        Parameters
        ----------
        agent:
            The owning CodeAgent.
        messages:
            Full message list for this call.
        tools:
            OpenAI-format tool schemas.
        tool_choice:
            Tool choice mode.
        fallback:
            Zero-argument callable that performs the real API call.

        Returns
        -------
        The raw LLM response object.
        """
        return fallback()
