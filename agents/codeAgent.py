import json
import time
import uuid
import secrets
import os
import logging
import sys
import threading
import traceback as tb
from pathlib import Path
from typing import Any, Optional, List, Tuple

from core.agent import Agent
from core.events import AgentEvent, EventType, EventSink
from core.llm import HelloAgentsLLM
from core.message import Message
from core.config import Config
from core.context_engine.context_builder import ContextBuilder
from core.context_engine.trace_logger import create_trace_logger
from core.env import load_env
from core.response_parser import (
    extract_content,
    extract_reasoning_content,
    extract_usage,
    extract_tool_calls,
    extract_response_meta,
    ensure_json_input,
)

load_env()
from core.context_engine.history_manager import HistoryManager
from core.context_engine.summary_compressor import create_summary_generator
from core.session_store import save_session_snapshot, load_session_snapshot
from core.team_engine.display_mode import resolve_teammate_mode
from tools.registry import ToolRegistry
from core.worktree.manager import WorktreeManager, WorktreeError
from core.runtime.services import RuntimeServices
from core.runtime.middleware import PolicyMiddlewarePipeline
from core.runtime.service_mcp import MCPService
from core.runtime.service_teams import TeamService
from core.runtime.service_sessions import SessionService
from core.runtime.service_sandbox import SandboxService
from core.runtime.service_worktree import WorktreeService
from core.runtime.service_background import BackgroundService
from core.runtime.service_style import StyleService
from core.runtime.service_hooks import HookService
from core.runtime.service_vcr import VCRService
from tools.permission_gate import create_permission_gate
from utils import setup_logger
from core.skills.skill_loader import SkillLoader
from core.tool_bootstrap import ToolBootstrap, register_team_tools


class CodeAgent(Agent):
    """
    Code Agent - 基于 ReAct 的代码助手
    
    上下文工程改造（按方案 D3）：
    - 使用 HistoryManager 管理会话历史
    - ReAct 每一步同步写入 assistant/tool 消息到 history
    - 支持压缩触发和 Summary 生成
    """
    # Tool allowlists — class-level fallbacks, overridden by mandatory policies at runtime.
    DELEGATION_ALLOWED_TOOLS: set[str] = set()
    PLAN_MODE_TOOLS = {
        "Read", "Grep", "Glob", "LS", "TodoWrite",
        "TaskOutput", "EnterPlanMode", "ExitPlanMode", "AskUser",
        "ToolSearch",
    }
    
    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        tool_registry: ToolRegistry,
        project_root: str,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        logger=None,
        event_sink=None,  # core.events.EventSink — UI decoupling
        hosted_tool_authority=None,  # trusted Worker binding, never a request field
    ):
        super().__init__(name, llm, system_prompt=system_prompt, config=config)
        self.project_root = project_root
        self._original_project_root = project_root
        # Keep the shared project skill/plugin catalog stable when a Web
        # session rebinds only its tools to a per-session workspace.
        self._skill_catalog_root = str(Path(project_root).resolve())
        self.tool_registry = tool_registry
        self._sandbox_mode = str(getattr(self.config, "sandbox_mode", "local_trusted")).strip().lower()
        if self._sandbox_mode not in {"local_trusted", "docker", "boxlite", "e2b"}:
            raise ValueError("Unknown AGENT_SANDBOX_MODE; expected local_trusted, docker, boxlite or e2b")
        if getattr(self.config, "browser_enabled", False) and (
            self._sandbox_mode != "local_trusted" or hosted_tool_authority is not None
            or getattr(self.config, "require_isolated_execution", False)
        ):
            raise PermissionError("Host BrowserUse is only allowed for explicitly trusted local sessions")
        if getattr(self.config, "require_isolated_execution", False) and self._sandbox_mode == "local_trusted":
            raise PermissionError("This deployment requires isolated execution; local_trusted is forbidden")
        if hosted_tool_authority is not None and self._sandbox_mode == "local_trusted":
            raise PermissionError("Hosted Agent tools require an isolated sandbox, not local_trusted")
        self._hosted_tool_authority = hosted_tool_authority
        if self._sandbox_mode != "local_trusted" and getattr(self.config, "enable_agent_teams", False):
            raise ValueError("Isolated sandbox mode does not support AgentTeams yet")
        self.logger = logger or setup_logger(
            name=f"agent.{self.name}",
            level=self.config.log_level,
        )
        self.last_response_raw: Optional[Any] = None
        self.llm_stream_callback = None
        self.last_turn_status = 'completed'
        self.last_turn_error_kind = None
        self._interrupt_requested = threading.Event()
        # Structured event sink — UI adapters subscribe here instead of
        # overriding _console().  Default is no-op (headless/testing safe).
        if event_sink is None:
            from core.events import EventSink
            event_sink = EventSink()
        self.event_sink = event_sink
        self.max_steps = int(getattr(self.config, "max_steps", 50))
        self.verbose = bool(self.config.debug)
        self.console_verbose = bool(self.config.show_react_steps)
        self.console_progress = bool(self.config.show_progress)
        self.interactive = os.getenv("AGENT_INTERACTIVE", "true").lower() in {"1", "true", "yes", "y", "on"}
        self.enable_agent_teams = bool(getattr(self.config, "enable_agent_teams", False))
        self.team_store_dir = str(getattr(self.config, "agent_teams_store_dir", ".teams") or ".teams")
        self.task_store_dir = str(getattr(self.config, "agent_tasks_store_dir", ".tasks") or ".tasks")
        self.teammate_mode = str(getattr(self.config, "teammate_mode", "auto") or "auto")
        self.teammate_runtime_mode, self.teammate_mode_warning = resolve_teammate_mode(self.teammate_mode)
        self.delegate_mode = bool(getattr(self.config, "delegate_mode", False))
        if self.teammate_mode_warning:
            self.logger.warning(self.teammate_mode_warning)
        # TeamService creates TeamManager when enabled.
        self.team_manager = None
        self.logger.info(
            "AgentTeams enabled=%s, team_store_dir=%s, task_store_dir=%s, teammate_mode=%s, teammate_runtime_mode=%s, delegate_mode=%s",
            self.enable_agent_teams,
            self.team_store_dir,
            self.task_store_dir,
            self.teammate_mode,
            self.teammate_runtime_mode,
            self.delegate_mode,
        )

        # ── Core initialisation ──
        self._init_core()

        # Framework-owned state with explicit lifecycle; no AgentFeature ABI.
        self._runtime_services = RuntimeServices(self)
        self._runtime_services.register("worktree", WorktreeService)
        self._runtime_services.register("mcp", MCPService)
        self._runtime_services.register("teams", TeamService, requires=("mcp",))
        self._runtime_services.register("background", BackgroundService)
        self._runtime_services.register("style", StyleService)
        self._runtime_services.register("hooks", HookService)
        self._runtime_services.register("vcr", VCRService)
        self._runtime_services.register("session", SessionService)
        self._runtime_services.register("sandbox", SandboxService)
        self._worktree_manager = None
        self._background_runner = None
        self._hook_system_messages = []
        self._hook_session_context = []
        self._tool_interceptors = ()
        self._model_interceptors = ()

        self._policy_middleware = PolicyMiddlewarePipeline(self)
        self._policy_middleware.initialize()

        try:
            # Hosted identities may only provision an isolated shell; never
            # initialize host file, plugin, MCP, hook or session resources.
            if self._hosted_tool_authority is None:
                for service in (
                    "worktree", "mcp", "teams", "background",
                    "style", "hooks", "vcr", "session",
                ):
                    self._runtime_services.ensure(service)

            self._init_tools()
            self._runtime_services.post_start()
            if self._hosted_tool_authority is None:
                self._tool_interceptors = (self._runtime_services.get("hooks"),)
                self._model_interceptors = (self._runtime_services.get("vcr"),)

            # Deferred catalog sees fully registered tools.
            self._configure_tool_discovery()
            if self._hosted_tool_authority is not None:
                self.tool_registry.bind_hosted_authority(self._hosted_tool_authority)

            # ── Session ID fallback ──
            if not hasattr(self, "_session_id"):
                self._session_id = uuid.uuid4().hex[:12]

            self.trace_logger = create_trace_logger()
            self._system_messages_logged = False
            self._run_id = 0
            self._system_messages_override: Optional[List[dict]] = None
        except BaseException:
            self.close()
            raise

    def _init_core(self) -> None:
        """Initialise core infrastructure that services depend on."""
        # Each Agent owns its circuit-breaker policy.
        self.tool_registry.configure(self.config)

        # Summary generator
        summary_generator = create_summary_generator(
            llm=self.llm,
            config=self.config,
            verbose=self.verbose,
        )

        # History manager
        self.history_manager = HistoryManager(
            config=self.config,
            summary_generator=summary_generator,
        )

        # Skills
        self._skill_loader = SkillLoader(self._skill_catalog_root)
        self._skills_prompt = ""
        if self._hosted_tool_authority is None:
            self._refresh_skills_prompt()

        # MCP prompt placeholder (populated later in _init_tools)
        self._mcp_tools_prompt = ""
        self._mcp_clients = []

        # Permission gate
        self._permission_gate = create_permission_gate(
            project_root=self.project_root,
        )
        self._inject_permission_gate()

        # Context builder
        self.context_builder = ContextBuilder(
            tool_registry=self.tool_registry,
            project_root=self.project_root,
            system_prompt_override=self.system_prompt,
            mcp_tools_prompt=self._mcp_tools_prompt,
            skills_prompt=self._skills_prompt,
        )

    def _init_tools(self) -> None:
        """Register built-in and MCP tools via ToolBootstrap auto-discovery."""
        bootstrap = ToolBootstrap(
            registry=self.tool_registry,
            project_root=self.project_root,
        )
        self._tool_bootstrap = bootstrap

        # Register dependency providers — tools receive these via constructor injection
        bootstrap.provide("code_agent", self)
        bootstrap.provide("team_manager", self.team_manager)
        bootstrap.provide("background_runner", self._background_runner)
        bootstrap.provide("skill_loader", self._skill_loader)
        bootstrap.provide("main_llm", self.llm)
        bootstrap.provide("config", self.config)
        bootstrap.provide("tool_registry", self.tool_registry)
        bootstrap.provide("event_sink", self.event_sink)
        bootstrap.provide("interactive", self.interactive)
        bootstrap.provide("worktree_manager", self._worktree_manager)

        # In hosted mode even constructing host-local tool classes is denied;
        # provision the isolated shell adapter only, never host plugins.
        if self._hosted_tool_authority is None:
            registered = bootstrap.discover_and_register()
            self.logger.debug("Auto-registered %d built-in tools: %s", len(registered), registered)

        # Team tools (only when AgentTeams is enabled)
        if self.enable_agent_teams and self.team_manager:
            team_registered = register_team_tools(bootstrap)
            self.logger.debug("Registered %d team tools: %s", len(team_registered), team_registered)

        if getattr(self.config, "browser_enabled", False):
            from tools.builtin.browser_use import BrowserUseTool
            self._browser_tool = BrowserUseTool(project_root=Path(self.project_root), code_agent=self)
            self.tool_registry.register_tool(self._browser_tool, owner="builtin")

        if self._sandbox_mode != "local_trusted":
            self._runtime_services.ensure("sandbox")

        # MCP prompt sync (tools registered by MCPFeature.init)
        self.context_builder.set_mcp_tools_prompt(
            "" if self._sandbox_mode != "local_trusted" else self._mcp_tools_prompt
        )

    def _configure_tool_discovery(self) -> None:
        """Bind mandatory modes, then optionally enable a bounded tool catalog.

        This is a context/UX optimization, never hosted authorization: the
        existing plan/delegate hook interceptors and sandbox gate remain mandatory.
        """
        def shared_mode_allows(name: str) -> bool:
            return not self._in_plan_mode or name in self.PLAN_MODE_TOOLS

        def leader_mode_allows(name: str) -> bool:
            return not self.delegate_mode or self._is_tool_allowed_in_delegate_mode(name)

        self.tool_registry.configure_mode_guards(
            shared=shared_mode_allows, local=leader_mode_allows,
        )
        self._tool_discovery_state = None
        if not getattr(self.config, "tool_deferred_discovery_enabled", False):
            return
        if self._sandbox_mode != "local_trusted":
            self.logger.warning("Deferred tool discovery disabled in isolated sandbox mode")
            return
        from core.runtime.tool_catalog import ToolDiscoveryState, ToolSearchTool

        def mode_allows(name: str) -> bool:
            return shared_mode_allows(name) and leader_mode_allows(name)

        state = ToolDiscoveryState(
            max_promoted=getattr(self.config, "tool_discovery_max_promoted", 18),
        )
        self.tool_registry.register_tool(
            ToolSearchTool(self.tool_registry, state, can_promote=mode_allows),
        )
        self.tool_registry.enable_deferred_discovery(state)
        self._tool_discovery_state = state
        self.context_builder.set_mcp_tools_prompt(
            "Additional MCP, team and plugin tools are deferred. "
            "Call ToolSearch with a specific name or capability to discover "
            "tools before invoking them. Discovery does not grant permissions."
        )

    # ------------------------------------------------------------------
    # Worktree session isolation
    # ------------------------------------------------------------------

    def enter_worktree(self, name: str | None = None, path: str | None = None) -> None:
        """Switch the session's project_root to a worktree directory.

        Called by EnterWorktreeTool after git worktree creation/lookup.
        All subsequent tool operations (Read/Write/Edit/Bash/...) will
        target the worktree directory automatically.

        Parameters
        ----------
        name:
            The worktree name. Used for logging only (the actual switch
            uses *path*).
        path:
            Absolute filesystem path of the worktree.
        """
        if self._active_worktree is not None:
            raise WorktreeError(
                "CONFLICT",
                f"Already in worktree '{self._active_worktree.get('name')}'. "
                "ExitWorktree first before entering another.",
            )

        wt_path = Path(path).resolve() if isinstance(path, str) else Path(name).resolve() if name else None
        if wt_path is None:
            raise WorktreeError("INVALID_PARAM", "name or path required")

        # Resolve the entry from the worktree manager if possible.
        entry = None
        try:
            if path:
                entry = self._worktree_manager.get_by_path(path)
        except WorktreeError:
            pass

        self._active_worktree = entry or {"name": name or str(wt_path.name), "path": str(wt_path)}

        # Refresh tools and runtime components with new project_root.
        self._rebind_project_root(str(wt_path))

        self.logger.info(
            "Entered worktree '%s' at %s",
            self._active_worktree.get("name"),
            self.project_root,
        )

    def exit_worktree(self, action: str = "keep", discard_changes: bool = False) -> None:
        """Restore the session's project_root to the original directory.

        Called by ExitWorktreeTool.  This only restores the project_root;
        the tool handles git cleanup (keep/remove) independently.
        """
        if self._active_worktree is None:
            return

        wt_name = self._active_worktree.get("name", "unknown")
        self._active_worktree = None

        # Refresh tools and runtime components with original project_root.
        self._rebind_project_root(str(self._original_project_root))

        self.logger.info("Exited worktree '%s', restored to %s", wt_name, self.project_root)

    # ------------------------------------------------------------------
    # Plan mode
    # ------------------------------------------------------------------

    def enter_plan_mode(self) -> None:
        """Switch to plan-only mode. Only read-only tools are available."""
        self._in_plan_mode = True
        self._plan_text = None
        self.logger.info("Entered plan mode")

    def exit_plan_mode(self, plan: str) -> None:
        """Exit plan mode, restore full tools, inject plan into context.

        Also writes ``PLAN.md`` to the project root so the plan is
        persistent, version-controllable, and shareable (Pi philosophy).
        The plan is still injected into context for immediate execution.
        """
        self._in_plan_mode = False
        self._plan_text = plan.strip()

        # Append a TodoWrite reminder so the LLM tracks plan progress.
        steps = self._extract_plan_steps(plan)
        if steps:
            todo_lines = ["Use TodoWrite to track the plan:"]
            for i, step in enumerate(steps, 1):
                todo_lines.append(f"  {i}. [pending] {step}")
            self._plan_text += "\n\n" + "\n".join(todo_lines)

        # Write PLAN.md to project root (Pi-inspired: persistent, shareable plan)
        self._write_plan_md(plan.strip(), steps)

        self.logger.info("Exited plan mode, plan length=%d, steps=%d", len(self._plan_text), len(steps))

    def _write_plan_md(self, plan_text: str, steps: list[str]) -> None:
        """Write the plan to PLAN.md in the project root."""
        from datetime import datetime
        plan_path = Path(self.project_root) / "PLAN.md"
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            content_parts = [
                f"# Plan — {timestamp}",
                "",
                plan_text,
            ]
            if steps:
                content_parts.append("")
                content_parts.append("## Steps")
                for i, step in enumerate(steps, 1):
                    content_parts.append(f"{i}. {step}")
            plan_path.write_text("\n".join(content_parts) + "\n", encoding="utf-8")
            self.logger.info("Plan written to %s", plan_path)
        except OSError as exc:
            self.logger.warning("Failed to write PLAN.md: %s", exc)

    # ------------------------------------------------------------------
    # Model switching (Claude Code /model equivalent)
    # ------------------------------------------------------------------

    def switch_model(self, *, model: str | None = None, provider: str | None = None) -> None:
        """Switch the active LLM model mid-conversation.

        1. If *model* matches a named profile (from ``MODEL_PROFILES``),
           use the profile's credentials (api_key, base_url, provider).
        2. Otherwise, use *model* as a raw model ID with the current
           credentials.

        The tool registry and all other agent state are unaffected.
        """
        previous = self.llm.model

        # Check named profiles first.
        from core.model_profiles import load_model_profiles
        profiles = getattr(self, "_model_profiles", None)
        if profiles is None:
            profiles = load_model_profiles()
            self._model_profiles = profiles

        profile = profiles.get((model or "").lower()) if model else None
        if profile:
            self.llm = HelloAgentsLLM(
                model=profile.model,
                api_key=profile.api_key or self.llm.api_key,
                base_url=profile.base_url or self.llm.base_url,
                provider=profile.provider or self.llm.provider,
                temperature=self.llm.temperature,
                max_tokens=self.llm.max_tokens,
                timeout=self.llm.timeout,
            )
            self.logger.info(
                "Switched model: %s → %s (profile: %s, provider: %s)",
                previous, profile.model, profile.name, profile.provider,
            )
            provider_name = profile.provider or self.llm.provider
            model_name = profile.model
        else:
            new_model = model or self.llm.model
            new_provider = provider or self.llm.provider
            self.llm = HelloAgentsLLM(
                model=new_model,
                api_key=self.llm.api_key,
                base_url=self.llm.base_url,
                provider=new_provider,
                temperature=self.llm.temperature,
                max_tokens=self.llm.max_tokens,
                timeout=self.llm.timeout,
            )
            self.logger.info("Switched model: %s → %s (provider: %s)", previous, new_model, new_provider)
            provider_name = new_provider
            model_name = new_model

        # Record model change in session tree
        if hasattr(self, "history_manager"):
            self.history_manager.append_model_change(
                provider=provider_name,
                model_id=model_name,
            )

        # Observers belong to explicitly owned runtime components.
        for observer in (
            getattr(self, "_policy_middleware", None),
            getattr(self, "_runtime_services", None) and self._runtime_services.get("session"),
        ):
            if observer is None:
                continue
            callback = getattr(observer, "on_model_changed", None)
            if callable(callback):
                try:
                    callback(self, previous, model_name)
                except Exception as exc:
                    self.logger.warning("Model-change observer unavailable: %s", exc)

    # ------------------------------------------------------------------
    # Output Styles
    # ------------------------------------------------------------------

    def _sync_output_style_to_context(self) -> None:
        """Push the current output style prompt into the ContextBuilder."""
        prompt = self._output_style_manager.get_current_prompt()
        self.context_builder.set_output_style_prompt(prompt)

    @property
    def output_style(self) -> str:
        """Return the currently active output style name."""
        return self._output_style_manager.get_current()

    def set_output_style(self, name: str) -> bool:
        """Set the active output style. Returns ``True`` on success."""
        ok = self._output_style_manager.set_current(name)
        if ok:
            self._sync_output_style_to_context()
            self.logger.info(
                "Output style set to %s", self._output_style_manager.get_current()
            )
        return ok

    def list_output_styles(self) -> dict[str, str]:
        """Return ``{name: description}`` for all available styles."""
        return self._output_style_manager.list_all()

    @staticmethod
    def _extract_plan_steps(plan: str) -> list[str]:
        """Extract numbered or bulleted steps from a plan text.

        Recognises lines like ``1. Do X``, ``- Do X``, ``* Do X``.
        Returns up to 10 steps.
        """
        import re
        steps: list[str] = []
        for line in plan.splitlines():
            stripped = line.strip()
            # Numbered: "1. Do X" or "1) Do X"
            m = re.match(r"^\d+[.)]\s+(.+)", stripped)
            if m:
                steps.append(m.group(1))
                continue
            # Bulleted: "- Do X" or "* Do X"
            m = re.match(r"^[-*]\s+(.+)", stripped)
            if m and len(m.group(1)) > 3:
                steps.append(m.group(1))
        return steps[:10]  # TodoWrite max is 10

    def _refresh_skills_prompt(self) -> None:
        refresh = os.getenv("SKILLS_REFRESH_ON_CALL", "true").lower() in {"1", "true", "yes", "y", "on"}
        if refresh:
            self._skill_loader.refresh_if_stale()
        elif not self._skills_prompt:
            self._skill_loader.scan()
        budget = int(os.getenv("SKILLS_PROMPT_CHAR_BUDGET", "12000"))
        self._skills_prompt = self._skill_loader.format_skills_for_prompt(budget)

    def _rebind_project_root(self, project_root: str) -> None:
        """Rebind path-aware runtime components after worktree root changes."""
        if self._sandbox_mode != "local_trusted":
            raise RuntimeError(
                "Worktree root switching is disabled in isolated sandbox mode; "
                "configure a new isolated session workspace instead"
            )
        root = Path(project_root).resolve()
        self.project_root = str(root)
        self._permission_gate = create_permission_gate(project_root=self.project_root)
        for tool in self.tool_registry.get_all_tools():
            if hasattr(tool, "_project_root"):
                tool._project_root = root
            if hasattr(tool, "_working_dir"):
                tool._working_dir = root
            if hasattr(tool, "_root"):
                tool._root = root
            tool._permission_gate = self._permission_gate
        if hasattr(self, "context_builder"):
            self.context_builder.project_root = self.project_root
        if hasattr(self, "_skill_loader"):
            self._skill_loader = SkillLoader(
                getattr(self, "_skill_catalog_root", self.project_root)
            )
            self._refresh_skills_prompt()
            if hasattr(self, "context_builder"):
                self.context_builder.set_skills_prompt(self._skills_prompt)
        if self.team_manager and hasattr(self.team_manager, "set_project_root"):
            self.team_manager.set_project_root(self.project_root)

    def _inject_permission_gate(self) -> None:
        """将 PermissionGate 注入到所有已注册的工具实例中。

        对于已有 permission_gate 属性的工具（如子代理复用），保留原值。
        """
        for tool in self.tool_registry.get_all_tools():
            if getattr(tool, "_permission_gate", None) is None:
                tool._permission_gate = self._permission_gate

    def interrupt(self) -> None:
        """Cancel future tool steps and signal the active Bash invocation.

        Bash can terminate its POSIX child process group cooperatively.
        Provider/MCP requests and other tools may still require timeout.
        """
        self._interrupt_requested.set()

    def run(self, input_text: str, **kwargs) -> str:
        """Run each turn with isolated configuration in the current thread."""
        # A verified principal and Bash-only registry do not yet supply a
        # durable hosted Session/Worker/Sandbox resource authorization contract.
        # Refuse hosted turns until the scoped runner is connected, rather
        # than accidentally executing the local SessionManager path.
        if getattr(self, "_hosted_tool_authority", None) is not None:
            raise PermissionError("Hosted Agent turns require an authenticated fenced worker runner")
        from core.runtime.config_scope import scoped_config
        with scoped_config(self.config):
            return self._run_scoped(input_text, **kwargs)

    def _run_scoped(self, input_text: str, **kwargs) -> str:
        """Compatibility adapter for isolated Agent turn lifecycle."""
        from core.runtime.turn_lifecycle import AgentTurnLifecycle
        return AgentTurnLifecycle(self).run(input_text, **kwargs)

    def close(self):
        """Idempotently dispose owned services, sandbox and tracing."""
        if getattr(self, "_closed", False):
            return
        self._closed = True

        browser_tool = getattr(self, "_browser_tool", None)
        if browser_tool is not None:
            self._browser_tool = None
            try:
                browser_tool.close()
            except Exception:
                self.logger.warning("Browser session teardown failed")

        runtime_services = getattr(self, "_runtime_services", None)
        if runtime_services is not None:
            runtime_services.close()
        # Only cover constructor failures before SandboxService was installed.
        sandbox_provider = getattr(self, "_sandbox_provider", None)
        if sandbox_provider is not None:
            try:
                close = getattr(sandbox_provider, "close", None)
                if callable(close):
                    close()
            except Exception:
                self.logger.warning("Isolated sandbox cleanup failed")
            self._sandbox_provider = None
        if getattr(self, "trace_logger", None):
            try:
                self.trace_logger.finalize()
            except Exception as exc:
                self.logger.warning("Trace finalize failed: %s", exc)
            self.trace_logger = None

    # =========================================================================
    # ReAct Core（Message List 自然累积模式）
    # =========================================================================

    # ------------------------------------------------------------------
    # ReAct 循环（拆分为 3 个子方法以提高可读性和可测试性）
    # ------------------------------------------------------------------

    def _react_loop(self, pending_input: str, show_raw: bool, trace_logger) -> str:
        """Compatibility adapter for the extracted AgentLoop orchestrator."""
        from core.runtime.agent_loop import AgentLoop
        return AgentLoop(self).run(pending_input, show_raw, trace_logger)

    # ------------------------------------------------------------------
    # ReAct 子方法
    # ------------------------------------------------------------------

    def _collect_runtime_blocks(self, step: int) -> None:
        """Collect core policy, resource status and approved hook context."""
        blocks: list[str] = []
        services = getattr(self, "_runtime_services", None)
        for name in ("mcp", "teams"):
            service = services.get(name) if services else None
            if service is not None:
                blocks.extend(service.runtime_blocks(self, step))
        for policy in self._policy_middleware.policies:
            runtime_blocks = getattr(policy, "runtime_blocks", None)
            if callable(runtime_blocks):
                blocks.extend(runtime_blocks(self, step))
        for name in ("background", "hooks"):
            service = services.get(name) if services else None
            if service is not None:
                blocks.extend(service.runtime_blocks(self, step))
        if blocks and hasattr(self.context_builder, "set_runtime_system_blocks"):
            self.context_builder.set_runtime_system_blocks(blocks)

    def _maybe_compress_history(self, pending_input: str, trace_logger, step: int, tools_schema=None) -> None:
        from core.runtime.context_runtime import ContextRuntime
        ContextRuntime(self).maybe_compress_history(pending_input, trace_logger, step, tools_schema)

    def _build_step_messages(self, trace_logger, step: int):
        from core.runtime.context_runtime import ContextRuntime
        return ContextRuntime(self).build_step_messages(trace_logger, step)

    def _invoke_llm_with_retry(
        self,
        messages, base_messages, tools_schema, tool_choice,
        show_raw: bool, trace_logger, step: int,
    ) -> dict | None:
        """调用 LLM，含一次空响应重试；不完整输出以结构化错误结束。"""
        empty_retry_used = False
        reasoning_content = None

        while True:
            self.event_sink.emit(AgentEvent(EventType.LLM_STARTED, {
                "step": step,
            }, step=step))

            from core.runtime.model_gateway import ModelGateway

            def _on_delta(kind: str, text: str) -> None:
                callback = getattr(self, "llm_stream_callback", None)
                if callback:
                    callback(kind, text, step)

            # Local latency metadata only; the private TraceLogger still owns
            # the richer model_output audit, while the Web projection never
            # sends message contents or tool schemas to the browser.
            started_model = time.monotonic()
            try:
                raw_response = ModelGateway(
                    self.llm, streaming=bool(getattr(self.config, "llm_streaming", True)),
                ).invoke(
                    messages=messages, tools_schema=tools_schema,
                    tool_choice=tool_choice, interceptors=self._model_interceptors,
                    agent=self, on_delta=_on_delta,
                )
            except Exception:
                trace_logger.log_event("model_call", {
                    "model": getattr(self.llm, "model", "unknown"),
                    "status": "error",
                    "duration_ms": int((time.monotonic() - started_model) * 1000),
                }, step=step)
                raise
            model_elapsed_ms = int((time.monotonic() - started_model) * 1000)
            if show_raw:
                self.last_response_raw = (
                    raw_response.model_dump()
                    if hasattr(raw_response, "model_dump")
                    else raw_response
                )

            response_text = extract_content(raw_response) or ""
            reasoning_content = extract_reasoning_content(raw_response)
            usage = extract_usage(raw_response)
            trace_logger.log_event("model_call", {
                "model": getattr(self.llm, "model", "unknown"),
                "status": "success",
                "duration_ms": model_elapsed_ms,
                "usage": usage,
            }, step=step)
            if usage and usage.get("total_tokens") is not None:
                self.history_manager.update_last_usage(usage["total_tokens"])
                # Budget tracking
                if hasattr(self, "_budget_tracker"):
                    self._budget_tracker.spend(usage["total_tokens"])

            self.event_sink.emit(AgentEvent(EventType.LLM_COMPLETED, {
                "step": step,
                "usage": usage or {},
            }, step=step))

            response_meta = extract_response_meta(raw_response)
            tool_calls = extract_tool_calls(raw_response)
            raw_dump = self._extract_raw_response(raw_response)
            trace_logger.log_event(
                "model_output",
                {
                    "raw": response_text,
                    "usage": usage,
                    "meta": response_meta,
                    "raw_response": raw_dump,
                    "tool_calls": tool_calls,
                },
                step=step,
            )

            # Incomplete provider stops invalidate both text and tool calls.
            # Keep evidence outside final history/tool execution so incomplete
            # but syntactically valid calls cannot cause side effects.
            from core.llm_recovery import ModelInvocationError, incomplete_response_kind
            incomplete_kind = incomplete_response_kind(response_meta.get("finish_reason"))
            if incomplete_kind:
                self.history_manager.append_assistant(
                    content=str(response_text),
                    metadata={"step": step, "action_type": "incomplete",
                              "error_kind": incomplete_kind,
                              "tool_calls": tool_calls},
                    reasoning_content=reasoning_content,
                )
                raise ModelInvocationError(incomplete_kind)

            if self.console_verbose and reasoning_content:
                display_reasoning = reasoning_content
                if len(display_reasoning) > 1200:
                    display_reasoning = display_reasoning[:1200] + "...(truncated)"
                self._console(f"\n🧠 Reasoning: {display_reasoning}\n")

            if tool_calls or (response_text and str(response_text).strip()):
                return {
                    "tool_calls": tool_calls,
                    "response_text": response_text,
                    "reasoning_content": reasoning_content,
                }

            # 重试一次并追加提示
            if not empty_retry_used:
                empty_retry_used = True
                hint = "上次 content 为空且未返回 tool_calls，请在 content 中回复最终答案，或使用工具调用。"
                messages = base_messages + [{"role": "user", "content": hint}]
                trace_logger.log_event(
                    "empty_response_retry",
                    {
                        "finish_reason": response_meta.get("finish_reason"),
                        "content_len": response_meta.get("content_len"),
                        "reasoning_len": response_meta.get("reasoning_len"),
                        "hint": hint,
                    },
                    step=step,
                )
                if self.console_verbose:
                    self._console("⚠️ LLM返回空响应，追加提示后重试一次")
                else:
                    self.logger.warning("LLM返回空响应，追加提示后重试一次")
                continue

            if self.console_verbose:
                self._console("❌ LLM返回空响应")
            else:
                self.logger.error("LLM返回空响应")
            trace_logger.log_event(
                "error",
                {
                    "stage": "llm_response",
                    "error_code": "INTERNAL_ERROR",
                    "message": "Empty response",
                    "meta": response_meta,
                },
                step=step,
            )
            from core.llm_recovery import ModelInvocationError
            raise ModelInvocationError("empty_response")

    def _execute_step_tools(
        self, tool_calls, response_text, reasoning_content, trace_logger, step: int,
    ) -> None:
        """Execute ordered tool batches; only explicitly safe reads may overlap.

        Events, traces and history remain on the owning Agent thread. Unsafe
        calls are barriers, even if the model returned several calls at once.
        """
        from core.runtime.tool_scheduler import plan_tool_batches, run_tool_batch
        from core.runtime.tool_contracts import error_envelope
        from core.runtime.tool_runtime import ToolRuntime
        from core.runtime.tool_scheduler import ToolBatch

        # Test/plugin callers may override _execute_tool at the instance level.
        # Preserve that compatibility while the framework-owned entrypoint
        # uses the explicit three-phase runtime.
        use_stages = "_execute_tool" not in self.__dict__
        if getattr(self, "_hosted_tool_authority", None) is not None and not use_stages:
            raise PermissionError("Hosted Agent execution cannot override the sealed tool dispatcher")
        staged_runtime = (
            ToolRuntime(
                self.tool_registry, self.logger,
                step_snapshot=getattr(self, "_model_tool_step_snapshot", None),
            ) if use_stages else None
        )
        if use_stages and not hasattr(self, "_last_tool_timing"):
            self._last_tool_timing = {}
        prepared = {}
        prepare_errors = {}

        for call in tool_calls:
            if not call.get("id"):
                call["id"] = f"call_{uuid.uuid4().hex}"
        assistant_content = str(response_text or "")
        self.history_manager.append_assistant(
            content=assistant_content,
            metadata={
                "step": step,
                "action_type": "tool_call",
                "tool_calls": tool_calls,
            },
            reasoning_content=reasoning_content,
        )
        self._log_message_write(
            trace_logger, "assistant", assistant_content,
            {"action_type": "tool_call", "tool_calls": tool_calls}, step,
        )

        parsed_calls = []
        for call_index, call in enumerate(tool_calls):
            raw_args = call.get("arguments") or {}
            tool_input, parse_err = ensure_json_input(raw_args)
            parsed_calls.append({
                "index": call_index,
                "name": call.get("name") or "unknown_tool",
                "id": call["id"],
                "input": tool_input,
                "parse_error": parse_err,
            })

        config = getattr(self, "config", None)
        enabled = bool(getattr(config, "tool_parallel_reads_enabled", False))
        max_workers = max(1, min(8, int(getattr(config, "tool_max_concurrency", 4))))
        hook_manager = getattr(self, "_hook_manager", None)
        hooks_active = bool(hook_manager and hook_manager.has_any_hooks)
        # Declarative hooks and any unknown interceptor are serial barriers.
        # Only the framework-owned HookService is reviewed for concurrent
        # read semantics. A test or local integration cannot gain parallel
        # permission by merely inserting an arbitrary interceptor.
        known_interceptors = all(
            type(interceptor) is HookService
            for interceptor in getattr(self, "_tool_interceptors", ())
        )
        policy_allows_parallel = (
            enabled and not hooks_active and known_interceptors
            and not getattr(self, "delegate_mode", False)
            and not getattr(config, "budget_enforce", False)
        )

        def safe_for_parallel(item: dict) -> bool:
            if not policy_allows_parallel or item["parse_error"]:
                return False
            get_tool = getattr(self.tool_registry, "get_tool", None)
            tool = get_tool(item["name"]) if callable(get_tool) else None
            classifier = getattr(tool, "is_concurrency_safe", None)
            return bool(classifier(item["input"])) if callable(classifier) else False

        batches = plan_tool_batches(
            parsed_calls, safe_for_parallel,
            max_workers=max_workers if policy_allows_parallel else 1,
        )

        def execute(item: dict) -> tuple[str, str | None, str | None, int]:
            """Only dispatch is allowed to run inside a worker thread."""
            if item["parse_error"]:
                result = error_envelope(
                    "INVALID_PARAM", f"Tool arguments parse error: {item['parse_error']}",
                    item["input"] if isinstance(item["input"], dict) else {},
                )
                return json.dumps(result, ensure_ascii=False), None, None, 0
            dispatch_started = time.monotonic()
            try:
                if self._interrupt_requested.is_set():
                    result = json.dumps(error_envelope(
                        "INTERRUPTED", "Tool not executed: user interrupted", {},
                    ), ensure_ascii=False)
                elif item["index"] in prepare_errors:
                    raise prepare_errors[item["index"]]
                elif use_stages:
                    from core.runtime.cancellation import scoped_tool_cancellation
                    with scoped_tool_cancellation(self._interrupt_requested):
                        result = staged_runtime.dispatch(prepared[item["index"]])
                else:
                    result = str(self._execute_tool(item["name"], item["input"]))
                return result, None, None, int((time.monotonic() - dispatch_started) * 1000)
            except Exception as exc:
                result = error_envelope("EXECUTION_ERROR", str(exc), item["input"])
                return json.dumps(result, ensure_ascii=False), str(exc), tb.format_exc(), int((time.monotonic() - dispatch_started) * 1000)

        for batch in batches:
            interrupted = self._interrupt_requested.is_set()
            prepared = {}
            prepare_errors = {}
            if use_stages and not interrupted:
                # All hook pre-callbacks and policy decisions run on the owner
                # thread, before the worker pool starts any I/O.
                for item in batch.calls:
                    if item["parse_error"]:
                        continue
                    try:
                        prepared[item["index"]] = staged_runtime.prepare(
                            item["name"], item["input"], agent=self,
                            interceptors=getattr(self, "_tool_interceptors", ()),
                            emit_messages=self._hook_system_messages.extend,
                        )
                    except Exception as exc:
                        prepare_errors[item["index"]] = exc
            # Updated inputs can change which concurrency guarantees hold.
            # Fail closed: if any changed call is not explicitly safe, serial.
            actual_parallel = batch.concurrent and not interrupted
            if use_stages and actual_parallel:
                for item in batch.calls:
                    prep = prepared.get(item["index"])
                    tool = self.tool_registry.get_tool(item["name"])
                    if (prep is None or prep.blocked_output is not None
                            or tool is None or not tool.is_concurrency_safe(prep.parameters)):
                        actual_parallel = False
                        break
            mode = "parallel" if actual_parallel else "serial"
            trace_logger.log_event("tool_batch_started", {
                "mode": mode, "count": len(batch.calls),
                "tools": [item["name"] for item in batch.calls],
                "max_workers": min(max_workers, len(batch.calls)) if mode == "parallel" else 1,
                "interrupted": interrupted,
            }, step=step)
            start = time.monotonic()

            if not interrupted:
                # Emit all starts before launching a concurrent batch. The UI
                # then sees the actual overlap instead of fake serial starts.
                for item in batch.calls:
                    if item["parse_error"]:
                        continue
                    trace_logger.log_event("tool_call", {
                        "tool": item["name"], "args": item["input"],
                        "tool_call_id": item["id"], "call_index": item["index"],
                    }, step=step)
                    self.event_sink.emit(AgentEvent(EventType.TOOL_STARTED, {
                        "tool": item["name"], "input": item["input"],
                        "tool_call_id": item["id"],
                    }, step=step))
                    if self.console_verbose:
                        self._console(f"\n🎬 Action: {item['name']}[{item['input']}]\n")
                    elif self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug("Action: %s %s", item["name"], item["input"])

                outcomes = run_tool_batch(
                    ToolBatch(batch.calls, concurrent=actual_parallel),
                    execute, max_workers=max_workers,
                )
            else:
                # Complete all announced tool_call IDs, even on interruption:
                # leaving unmatched IDs would make the next model call invalid.
                outcomes = [
                    (json.dumps(error_envelope(
                        "INTERRUPTED", "Tool not executed: user interrupted", item["input"],
                    ), ensure_ascii=False), None, None, 0)
                    for item in batch.calls
                ]

            errors = 0
            for item, (observation, execution_error, error_trace, call_duration_ms) in zip(batch.calls, outcomes):
                tool_name, tool_call_id, tool_input = item["name"], item["id"], item["input"]
                if use_stages and not interrupted and item["index"] in prepared and execution_error is None:
                    try:
                        observation = staged_runtime.publish(
                            prepared[item["index"]], observation,
                            agent=self,
                            emit_messages=self._hook_system_messages.extend,
                            record_timing=lambda name, ms: self._last_tool_timing.__setitem__(name, ms),
                        )
                    except Exception:
                        execution_error = "Tool result hook failed"
                        error_trace = None
                        observation = json.dumps(error_envelope(
                            "EXECUTION_ERROR", execution_error, {},
                        ), ensure_ascii=False)
                if item["parse_error"]:
                    errors += 1
                    trace_logger.log_event("error", {
                        "stage": "tool_call_parse", "error_code": "INVALID_PARAM",
                        "message": str(item["parse_error"]),
                        "tool": tool_name, "tool_call_id": tool_call_id,
                    }, step=step)
                elif interrupted:
                    errors += 1
                    trace_logger.log_event("error", {
                        "stage": "tool_execution", "error_code": "INTERRUPTED",
                        "tool": tool_name, "tool_call_id": tool_call_id,
                    }, step=step)
                elif execution_error is not None:
                    errors += 1
                    trace_logger.log_event("error", {
                        "stage": "tool_execution", "error_code": "EXECUTION_ERROR",
                        "message": execution_error, "tool": tool_name,
                        "tool_call_id": tool_call_id, "traceback": error_trace,
                    }, step=step)
                else:
                    try:
                        payload = json.loads(observation)
                        trace_logger.log_event("tool_result", {
                            "tool": tool_name, "tool_call_id": tool_call_id,
                            "call_index": item["index"], "duration_ms": call_duration_ms,
                            "result": payload,
                        }, step=step)
                    except (json.JSONDecodeError, TypeError):
                        trace_logger.log_event("tool_result", {
                            "tool": tool_name, "tool_call_id": tool_call_id,
                            "call_index": item["index"], "duration_ms": call_duration_ms,
                            "result": {"text": observation},
                        }, step=step)

                if not item["parse_error"] and not interrupted:
                    try:
                        status = json.loads(observation).get("status", "success")
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        status = "success"
                    if status == "error":
                        errors += 1 if execution_error is None else 0
                    self.event_sink.emit(AgentEvent(EventType.TOOL_COMPLETED, {
                        "tool": tool_name, "tool_call_id": tool_call_id,
                        "status": status, "output": observation, "input": tool_input,
                    }, step=step))

                self.history_manager.append_tool(
                    tool_name=tool_name, raw_result=observation,
                    metadata={"step": step, "tool_call_id": tool_call_id},
                    project_root=self.project_root,
                )
                self._log_message_write(
                    trace_logger, "tool", observation,
                    {"tool_name": tool_name, "tool_call_id": tool_call_id}, step,
                )
                if self.console_verbose:
                    display_obs = observation[:300] + "..." if len(observation) > 300 else observation
                    self._console(f"\n👀 Observation: {display_obs}\n")
                elif self.logger.isEnabledFor(logging.DEBUG):
                    display_obs = observation[:300] + "..." if len(observation) > 300 else observation
                    self.logger.debug("Observation: %s", display_obs)

            trace_logger.log_event("tool_batch_completed", {
                "mode": mode, "count": len(batch.calls), "errors": errors,
                "duration_ms": int((time.monotonic() - start) * 1000),
                "interrupted": interrupted,
            }, step=step)

    # =========================================================================
    # 辅助方法
    # =========================================================================
    
    def _log_message_write(self, trace_logger, role: str, content: str, metadata: dict, step: int = 0):
        """辅助：记录消息写入到 trace"""
        trace_logger.log_event("message_written", {
            "role": role,
            "content": content,
            "metadata": metadata,
        }, step=step)

    def _log_system_messages_if_needed(self, trace_logger) -> None:
        if self._system_messages_logged or not trace_logger:
            return
        system_messages = self._get_system_messages_for_run()
        trace_logger.log_system_messages(system_messages)
        self._system_messages_logged = True

    def _get_system_messages_for_run(self) -> List[dict]:
        # Session snapshots are untrusted serialized data. Never restore their
        # former system messages as current higher-priority instructions.
        return self.context_builder.get_system_messages()

    def _build_messages(self, history_messages: list[dict]) -> list[dict]:
        # The context builder applies the same provenance boundary in the
        # ordinary and post-compaction paths, without mutating saved history.
        return self.context_builder.build_messages(history_messages)

    # ------------------------------------------------------------------
    # Session management (multi-conversation)
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """Return the current session ID."""
        return self._session_id

    def _build_snapshot(self) -> dict:
        """Build the v2 session envelope through its dedicated assembler."""
        from core.runtime.session_snapshot import SessionSnapshotBuilder
        return SessionSnapshotBuilder(self).capture()

    def _auto_save_session(self) -> None:
        """Automatically persist the current session."""
        try:
            snapshot = self._build_snapshot()
            self._session_manager.save_session(self._session_id, snapshot)
        except Exception as exc:
            self.logger.warning("Auto-save session failed: %s", exc)

    def resume_session(self, session_id: str) -> bool:
        """Switch to a different session by ID.

        Saves the current session first, then loads the target.
        Returns ``True`` on success.
        """
        # Save current session first.
        self._auto_save_session()

        snapshot = self._session_manager.load_session(session_id)
        if snapshot is None:
            return False

        self._session_id = session_id
        self._system_messages_override = snapshot.get("system_messages") or []

        # Restore tree entries (v2) or legacy messages (v1)
        tree_entries = snapshot.get("history_entries")
        if tree_entries:
            self.history_manager.load_entries(tree_entries)
        else:
            history_items = snapshot.get("history_messages") or []
            self.history_manager.load_messages(history_items)

        self.tool_registry.import_read_cache(snapshot.get("read_cache") or {})
        if self.team_manager:
            self.team_manager.import_state(snapshot.get("teams_snapshot") or {})
        # Restore worktree state if the session was saved inside a worktree.
        worktree_state = snapshot.get("worktree_state")
        if isinstance(worktree_state, dict) and worktree_state.get("path"):
            try:
                self.enter_worktree(path=worktree_state["path"])
            except Exception:
                self.logger.warning(
                    "Failed to restore worktree from session: %s",
                    worktree_state.get("name", "unknown"),
                )
        return True

    def list_sessions(self) -> list[dict]:
        """Return a list of session metadata dicts."""
        sessions = self._session_manager.list_sessions()
        return [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "modified_at": s.modified_at,
                "message_count": s.message_count,
                "preview": s.preview,
            }
            for s in sessions
        ]

    def rename_session(self, title: str) -> bool:
        """Rename the current session."""
        return self._session_manager.rename_session(self._session_id, title)

    def resolve_session_id(self, identifier: str) -> str | None:
        """Resolve a session identifier (ID, index, prefix) to a session ID."""
        return self._session_manager.resolve_identifier(identifier)

    def save_session(self, path: str) -> None:
        """Legacy path-based save uses the same v2 snapshot as auto-save."""
        save_session_snapshot(path, self._build_snapshot())

    def load_session(self, path: str) -> None:
        """从快照恢复会话（scheme B）。"""
        snapshot = load_session_snapshot(path)
        self._system_messages_override = snapshot.get("system_messages") or []
        history_items = snapshot.get("history_messages") or []
        self.history_manager.load_messages(history_items)
        self.tool_registry.import_read_cache(snapshot.get("read_cache") or {})
        if self.team_manager:
            self.team_manager.import_state(snapshot.get("teams_snapshot") or {})
            if hasattr(self.context_builder, "set_runtime_system_blocks"):
                self.context_builder.set_runtime_system_blocks(
                    ["[Team Runtime]\n- Team state restored from session snapshot."]
                )
        # Restore worktree state if the session was saved inside a worktree.
        worktree_state = snapshot.get("worktree_state")
        if isinstance(worktree_state, dict) and worktree_state.get("path"):
            try:
                self.enter_worktree(path=worktree_state["path"])
            except Exception:
                self.logger.warning(
                    "Failed to restore worktree from session: %s",
                    worktree_state.get("name", "unknown"),
                )

    def _print_context_preview(
        self,
        messages: list[dict],
        max_messages: int = 10,
        content_limit: int = 200,
    ) -> None:
        if not messages:
            if self.console_verbose:
                self._console("（当前上下文为空）")
            else:
                self.logger.debug("当前上下文为空")
            return
        total = len(messages)
        preview = messages[:max_messages]
        if self.console_verbose:
            self._console(f"\n📌 当前上下文（最多显示 {max_messages} 条）")
        else:
            self.logger.debug("当前上下文（最多显示 %d 条）", max_messages)
        for msg in preview:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            content = str(content).replace("\n", "\\n")
            if len(content) > content_limit:
                content = content[:content_limit] + "...(truncated)"
            if self.console_verbose:
                self._console(f'message({role}, "{content}")')
            else:
                self.logger.debug('message(%s, "%s")', role, content)
        if total > max_messages:
            if self.console_verbose:
                self._console(f"...（其余 {total - max_messages} 条已省略）")
            else:
                self.logger.debug("其余 %d 条已省略", total - max_messages)

    def _console(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    @staticmethod
    def _format_runtime_system_blocks(
        events: list[dict],
        runtime_state: Optional[dict] = None,
        max_lines: int = 16,
    ) -> list[str]:
        from core.team_engine.runtime_view import TeamRuntimeView
        return TeamRuntimeView.format(events, runtime_state, max_lines)

    def _execute_tool(self, tool_name: str, tool_input: Any) -> str:
        """Execute one tool via the decoupled interceptor-aware runtime port."""
        from core.runtime.tool_runtime import ToolRuntime
        from core.runtime.cancellation import scoped_tool_cancellation
        if not hasattr(self, "_last_tool_timing"):
            self._last_tool_timing: dict[str, int] = {}
        with scoped_tool_cancellation(getattr(self, "_interrupt_requested", None)):
            return ToolRuntime(self.tool_registry, self.logger).execute(
                tool_name,
                tool_input,
                agent=self,
                interceptors=getattr(self, "_tool_interceptors", ()),
                emit_messages=self._hook_system_messages.extend,
                record_timing=lambda name, ms: self._last_tool_timing.__setitem__(name, ms),
            )

    def set_delegate_mode(self, enabled: bool) -> None:
        self.delegate_mode = bool(enabled)
        if hasattr(self.config, "delegate_mode"):
            self.config.delegate_mode = self.delegate_mode
        self.logger.info("Delegate mode set to %s", self.delegate_mode)

    def _is_tool_allowed_in_delegate_mode(self, tool_name: str) -> bool:
        if not self.delegate_mode:
            return True
        return str(tool_name or "") in self.DELEGATION_ALLOWED_TOOLS

    def _get_openai_tools_for_current_mode(self) -> list[dict[str, Any]]:
        # Bind discoveries only at a new model step. ToolSearch cannot unlock
        # another call in the same multi-tool response that requested it.
        activate = getattr(self.tool_registry, "activate_discovered_tools", None)
        if callable(activate):
            activate()
        capture = getattr(self.tool_registry, "capture_tool_step", None)
        if callable(capture):
            def allowed(name: str) -> bool:
                return (
                    (not self._in_plan_mode or name in self.PLAN_MODE_TOOLS)
                    and (not self.delegate_mode or self._is_tool_allowed_in_delegate_mode(name))
                )
            snapshot = capture(allowed=allowed)
            self._model_tool_step_snapshot = snapshot
            return snapshot.model_schemas()
        # Compatibility with registry ports supplied by older integrations.
        self._model_tool_step_snapshot = None
        tools = self.tool_registry.get_openai_tools()
        filtered: list[dict[str, Any]] = []
        for item in tools:
            function = item.get("function") if isinstance(item, dict) else None
            name = function.get("name") if isinstance(function, dict) else ""
            if self._in_plan_mode and str(name) not in self.PLAN_MODE_TOOLS:
                continue
            if self.delegate_mode and not self._is_tool_allowed_in_delegate_mode(str(name or "")):
                continue
            filtered.append(item)
        return filtered

    @staticmethod
    def _extract_raw_response(raw_response: Any) -> dict:
        """将原始响应转换为可序列化结构（用于 trace 记录）"""
        try:
            if hasattr(raw_response, "model_dump"):
                return raw_response.model_dump()
            if hasattr(raw_response, "dict"):
                return raw_response.dict()
            if isinstance(raw_response, dict):
                return raw_response
        except Exception:
            pass
        return {"raw": str(raw_response)}
    
    # =========================================================================
    # 兼容 Agent 基类接口（使用 HistoryManager）
    # =========================================================================
    
    def add_message(self, message: Message):
        """兼容旧接口：添加消息到历史"""
        if message.role == "user":
            self.history_manager.append_user(message.content, message.metadata)
        elif message.role == "assistant":
            self.history_manager.append_assistant(message.content, message.metadata)
        elif message.role == "tool":
            # 注意：旧接口没有 tool_name，使用 metadata 中的值
            tool_name = (message.metadata or {}).get("tool_name", "unknown")
            self.history_manager.append_tool(
                tool_name, 
                message.content, 
                message.metadata,
                project_root=self.project_root,
            )
        elif message.role == "summary":
            self.history_manager.append_summary(message.content)
    
    def clear_history(self):
        """兼容旧接口：清空历史"""
        self.history_manager.clear()
    
    def get_history(self) -> List[Message]:
        """兼容旧接口：获取历史"""
        return self.history_manager.get_messages()
