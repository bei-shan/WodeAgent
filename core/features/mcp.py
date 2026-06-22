"""MCPFeature — MCP server connection status tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent


class MCPFeature(AgentFeature):
    """Tracks MCP server connection status and provides /mcp command.

    Does NOT handle tool registration (that's in codeAgent._register_mcp_tools).
    This feature is for status visibility only.
    """

    name = "mcp_status"
    order = 25

    def init(self, agent: "CodeAgent") -> None:
        agent._mcp_status_cache: list[dict] = []

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        """Show pending MCP servers on first step."""
        if step != 1:
            return []
        try:
            from tools.mcp.loader import get_pending_server_names
            pending = get_pending_server_names()
            if pending:
                return [
                    f"[MCP] Pending servers: {', '.join(pending)}. "
                    f"They will be retried each step."
                ]
        except Exception:
            pass
        return []
