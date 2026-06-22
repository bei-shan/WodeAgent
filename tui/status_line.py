"""Status line — displays current model and mode in the prompt area."""

from __future__ import annotations

from prompt_toolkit.formatted_text import HTML


class StatusLine:
    """Builds the right-hand side of the prompt line showing model/mode.

    Usage::

        status = StatusLine(agent)
        session.prompt(status.prompt_html(), ...)
    """

    def __init__(self, code_agent):
        self._agent = code_agent

    def prompt_html(self) -> str:
        """Return HTML-formatted prompt with status indicators.

        Shows: current model, plan mode indicator, worktree indicator.
        """
        agent = self._agent
        parts: list[str] = ["<user>user</user> <arrow>➜</arrow> "]

        # Model indicator
        model = getattr(agent.llm, "model", "?")
        # Shorten model name for display
        short_model = model.rsplit("/", 1)[-1] if "/" in model else model
        if len(short_model) > 20:
            short_model = short_model[:17] + "..."
        parts.append(f"<model>[{short_model}]</model> ")

        # Plan mode indicator
        if getattr(agent, "_in_plan_mode", False):
            parts.append("<plan>[plan]</plan> ")

        # Output style indicator (only when non-default)
        style = getattr(agent, "output_style", "default")
        if style and style != "default":
            parts.append(f"<style>[style:{style}]</style> ")

        # Worktree indicator
        if getattr(agent, "_active_worktree", None):
            wt_name = agent._active_worktree.get("name", "?")
            parts.append(f"<worktree>[wt:{wt_name}]</worktree> ")

        return "".join(parts)

    @staticmethod
    def prompt_style() -> dict:
        """Return prompt_toolkit style dict for the status line."""
        return {
            "user": "#00ff00 bold",
            "arrow": "#0000ff",
            "model": "#888888 italic",
            "plan": "#ffff00 bold",
            "worktree": "#00ffff italic",
            "style": "#ff8800 italic",
        }
