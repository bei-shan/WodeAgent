"""MCPFeature — MCP server connection, tool registration, and status tracking.

Previously split between CodeAgent._register_mcp_tools and MCPFeature.
Now consolidated: this Feature handles the full MCP lifecycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent

logger = logging.getLogger(__name__)


class MCPFeature(AgentFeature):
    """Manages MCP server lifecycle: registration, retry, and status visibility."""

    name = "mcp_status"
    order = 25

    def init(self, agent: "CodeAgent") -> None:
        agent._mcp_clients = []
        agent._mcp_tools_prompt = ""

        # Register MCP tools
        try:
            from tools.mcp.loader import register_mcp_servers, format_mcp_tools_prompt
            clients, tools_meta = register_mcp_servers(
                agent.tool_registry, agent.project_root)
            agent._mcp_clients = clients
            agent._mcp_tools_prompt = format_mcp_tools_prompt(tools_meta)
            if tools_meta:
                agent.logger.info("MCP tools loaded: %d", len(tools_meta))
        except Exception as exc:
            agent.logger.warning("MCP registration skipped: %s", exc)

    def runtime_blocks(self, agent: "CodeAgent", step: int) -> list[str]:
        """Retry pending MCP servers each step + show status on first step."""
        blocks: list[str] = []

        # Retry pending servers
        try:
            from tools.mcp.loader import get_pending_server_names, retry_pending_server, format_mcp_tools_prompt
            pending = get_pending_server_names()
            if pending:
                for name in pending:
                    ok = retry_pending_server(agent.tool_registry, name, timeout=10.0)
                    if ok:
                        agent.logger.info("MCP server '%s' recovered", name)
                        # Rebuild prompt so LLM sees new tools
                        all_tools = agent.tool_registry.get_all_tools()
                        mcp_meta = [
                            {"name": t.name, "description": getattr(t, "description", "")}
                            for t in all_tools
                            if getattr(t, "name", "").startswith("mcp__")
                        ]
                        agent._mcp_tools_prompt = format_mcp_tools_prompt(mcp_meta)
        except Exception:
            pass

        # Show pending status on first step
        if step == 1:
            try:
                from tools.mcp.loader import get_pending_server_names
                pending = get_pending_server_names()
                if pending:
                    blocks.append(
                        f"[MCP] Pending servers: {', '.join(pending)}. "
                        f"They will be retried each step."
                    )
            except Exception:
                pass

        # Sync MCP prompt to context builder
        if agent._mcp_tools_prompt:
            agent.context_builder.set_mcp_tools_prompt(agent._mcp_tools_prompt)

        return blocks
