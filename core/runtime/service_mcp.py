"""Runtime-owned service migrated from core/features/mcp.py; no AgentFeature inheritance."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent

logger = logging.getLogger(__name__)


class MCPService:
    """Manages MCP server lifecycle: registration, retry, and status visibility."""

    name = "mcp_status"
    order = 25

    def start(self, agent: "CodeAgent") -> None:
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
            pending = get_pending_server_names(agent.tool_registry)
            if pending:
                for name in pending:
                    ok = retry_pending_server(agent.tool_registry, name, timeout=10.0)
                    if ok:
                        agent.logger.info("MCP server '%s' recovered", name)
                        # Rebuild prompt so LLM sees new tools
                        # MCP adapters are published as "<server>_<tool>"; their
                        # provenance, not a name prefix, identifies them.
                        owner_of = getattr(agent.tool_registry, "owner_of", None)
                        all_tools = agent.tool_registry.get_all_tools()
                        mcp_meta = [
                            {"name": t.name, "description": getattr(t, "description", "")}
                            for t in all_tools
                            if callable(owner_of)
                            and str(owner_of(t.name) or "").startswith("mcp:")
                        ]
                        agent._mcp_tools_prompt = format_mcp_tools_prompt(mcp_meta)
        except Exception:
            pass

        # Show pending status on first step
        if step == 1:
            try:
                from tools.mcp.loader import get_pending_server_names
                pending = get_pending_server_names(agent.tool_registry)
                if pending:
                    blocks.append(
                        f"[MCP] Pending servers: {', '.join(pending)}. "
                        f"They will be retried each step."
                    )
            except Exception:
                pass

        # Sync MCP prompt to context builder
        if getattr(agent.config, "tool_deferred_discovery_enabled", False):
            # Never leak deferred tool descriptions into the context. The
            # ToolSearch result and subsequent schema are the disclosure path.
            if getattr(agent, "_tool_discovery_state", None) is not None:
                agent.context_builder.set_mcp_tools_prompt(
                    "Additional MCP, team and plugin tools are deferred. "
                    "Use ToolSearch to discover them before invocation."
                )
        elif agent._mcp_tools_prompt:
            agent.context_builder.set_mcp_tools_prompt(agent._mcp_tools_prompt)

        return blocks

    def close(self, agent: "CodeAgent") -> None:
        """Release this Agent's share of its registry's MCP servers.

        Several Agents can share one ToolRegistry, so a closing Agent only drops
        its own reference: the last one out unregisters the servers' adapters and
        disconnects them.  Never sweeps another Agent's servers.

        A host with no shared registration still owns its clients outright, and
        those are closed here.
        """
        from tools.mcp.loader import release_mcp_registration
        registry = getattr(agent, "tool_registry", None)
        clients = list(getattr(agent, "_mcp_clients", []) or [])
        released = bool(registry is not None and clients) and release_mcp_registration(registry)
        if released:
            return
        for client in clients:
            try:
                client.close_sync()
            except Exception as exc:
                logger.debug("MCP client close failed: %s", exc)
