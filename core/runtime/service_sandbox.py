"""Isolated Sandbox resource owner; tool allowlist is sealed before model use."""
from __future__ import annotations

import secrets


class SandboxService:
    """Single owner of the Docker, BoxLite or E2B provider and Bash adapter."""

    def start(self, agent) -> None:
        if agent._sandbox_mode != "local_trusted":
            # Both Docker and BoxLite seal the registry before the first model
            # step; no host Read/Write/Skill/MCP/plugin tool bypass is allowed.
            from core.sandbox.command_tool import SandboxBashTool
            if agent._sandbox_mode == "docker":
                from core.sandbox.docker_provider import DockerCommandProvider
                provider = DockerCommandProvider(
                    agent.project_root,
                    image=agent.config.sandbox_docker_image,
                    memory=agent.config.sandbox_memory,
                    cpus=agent.config.sandbox_cpus,
                    pids_limit=agent.config.sandbox_pids_limit,
                    snapshot_workspace=agent.config.sandbox_workspace_snapshot_enabled,
                )
            elif agent._sandbox_mode == "boxlite":
                from core.sandbox.boxlite_provider import BoxliteCommandProvider
                provider = BoxliteCommandProvider(
                    agent.project_root,
                    image=agent.config.sandbox_boxlite_image,
                    memory_mib=agent.config.sandbox_boxlite_memory_mib,
                    cpus=agent.config.sandbox_boxlite_cpus,
                    guest_stdin_enabled=agent.config.sandbox_boxlite_stdin_enabled,
                )
                if agent.config.sandbox_boxlite_warm_enabled:
                    provider.enable_warm_pool(
                        session_scope=secrets.token_hex(24),
                        max_replicas=1,
                        idle_timeout=agent.config.sandbox_boxlite_warm_idle_seconds,
                    )
            else:
                from core.sandbox.e2b_provider import E2BCommandProvider
                provider = E2BCommandProvider(
                    agent.project_root,
                    template=agent.config.sandbox_e2b_template,
                    sandbox_ttl=agent.config.sandbox_e2b_ttl_seconds,
                    capacity_limit=agent.config.sandbox_e2b_capacity,
                    single_gateway_ack=agent.config.sandbox_e2b_single_gateway_ack,
                    redis_ledger_enabled=agent.config.sandbox_e2b_redis_ledger_enabled,
                    deployment=agent.config.sandbox_e2b_deployment,
                )
            agent._sandbox_provider = provider
            agent.tool_registry.register_tool(SandboxBashTool(provider), owner="builtin")
            agent.tool_registry.restrict_to_tools({"Bash"})
            agent.logger.warning(
                "%s sandbox mode: only isolated Bash is exposed; "
                "host file, MCP, skill and subagent tools are denied",
                agent._sandbox_mode,
            )

    def close(self, agent) -> None:
        provider = getattr(agent, "_sandbox_provider", None)
        agent._sandbox_provider = None
        if provider is not None:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
