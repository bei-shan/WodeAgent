"""HookFeature — lifecycle hook system (SessionStart/PreToolUse/PostToolUse/SessionEnd)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class HookFeature(AgentFeature):
    """Manages lifecycle hooks configured via ``.mycode/hooks.json``.

    Hooks can block/modify tool calls, inject system messages,
    and set environment variables.
    """

    name = "hook"
    order = 85

    def init(self, agent: "CodeAgent") -> None:
        from core.hook_system import HookManager

        agent._hook_manager = HookManager(project_root=agent._original_project_root)
        agent._hook_system_messages: list[str] = []
        agent._hook_session_context: list[str] = []

    def post_init(self, agent: "CodeAgent") -> None:
        """Run SessionStart hooks after all features are initialised."""
        if agent._hook_manager.has_any_hooks:
            result = agent._hook_manager.run_session_start()
            agent._hook_system_messages.extend(result.system_messages)
            agent._hook_session_context.extend(result.additional_context)

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        blocks: list[str] = []
        if agent._hook_system_messages:
            blocks.extend(agent._hook_system_messages)
            agent._hook_system_messages.clear()
        if step == 1 and agent._hook_session_context:
            blocks.extend(agent._hook_session_context)
            agent._hook_session_context.clear()
        return blocks

    def pre_tool_use(
        self, agent: "CodeAgent", tool_name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not agent._hook_manager.has_any_hooks:
            return None
        result = agent._hook_manager.run_pre_tool_use(tool_name, tool_input)
        if result.blocked:
            return {"blocked": True, "reason": result.reason}
        out: dict[str, Any] = {}
        if result.system_messages:
            out["system_messages"] = result.system_messages
        if result.updated_input:
            out["updated_input"] = result.updated_input
        return out if out else None

    def post_tool_use(
        self, agent: "CodeAgent", tool_name: str, tool_input: dict[str, Any], result: str
    ) -> list[str]:
        if not agent._hook_manager.has_any_hooks:
            return []
        post_result = agent._hook_manager.run_post_tool_use(
            tool_name, tool_input, result
        )
        return post_result.system_messages

    def cleanup(self, agent: "CodeAgent") -> None:
        if agent._hook_manager.has_any_hooks:
            try:
                agent._hook_manager.run_session_end()
            except Exception:
                pass
