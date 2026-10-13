import json
import time
import uuid
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
from core.features import collect_all_features
from core.features.base import AgentFeature
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
    # Tool allowlists — class-level fallbacks, overridden by Features at runtime.
    DELEGATION_ALLOWED_TOOLS: set[str] = set()
    PLAN_MODE_TOOLS = {
        "Read", "Grep", "Glob", "LS", "TodoWrite",
        "TaskOutput", "EnterPlanMode", "ExitPlanMode", "AskUser",
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
    ):
        super().__init__(name, llm, system_prompt=system_prompt, config=config)
        self.project_root = project_root
        self._original_project_root = project_root
        self.tool_registry = tool_registry
        self._sandbox_mode = str(getattr(self.config, "sandbox_mode", "local_trusted")).strip().lower()
        if self._sandbox_mode not in {"local_trusted", "docker"}:
            raise ValueError("Unknown AGENT_SANDBOX_MODE; expected local_trusted or docker")
        if self._sandbox_mode == "docker" and getattr(self.config, "enable_agent_teams", False):
            raise ValueError("Docker sandbox mode does not support AgentTeams yet")
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
        # TeamManager is initialized by AgentTeamsFeature to keep feature lifecycle single-owned.
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

        # ── Features init (sets agent attributes) ──
        self._features: list[AgentFeature] = collect_all_features(self)
        for feat in self._features:
            feat.init(self)

        # ── Tool registration (needs feature deps like _background_runner) ──
        self._init_tools()

        # ── Features post_init (needs tools + context_builder ready) ──
        for feat in self._features:
            feat.post_init(self)

        # ── Session ID fallback ──
        if not hasattr(self, "_session_id"):
            import uuid
            self._session_id = uuid.uuid4().hex[:12]

        # Trace 日志（单实例贯穿 Agent 生命周期）
        self.trace_logger = create_trace_logger()
        self._system_messages_logged = False
        self._run_id = 0
        self._system_messages_override: Optional[List[dict]] = None

    def _init_core(self) -> None:
        """Initialise core infrastructure that features depend on."""
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
        self._skill_loader = SkillLoader(self.project_root)
        self._skills_prompt = ""
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

        # Auto-discover and register all non-team built-in tools
        registered = bootstrap.discover_and_register()
        self.logger.debug("Auto-registered %d built-in tools: %s", len(registered), registered)

        # Team tools (only when AgentTeams is enabled)
        if self.enable_agent_teams and self.team_manager:
            team_registered = register_team_tools(bootstrap)
            self.logger.debug("Registered %d team tools: %s", len(team_registered), team_registered)

        if self._sandbox_mode == "docker":
            # Install the isolated executor before the first model request.
            # Fail-closed registry policy also covers any tools registered
            # later by Features, MCP or custom clients.
            from core.sandbox.docker_provider import DockerCommandProvider
            from core.sandbox.command_tool import SandboxBashTool
            provider = DockerCommandProvider(
                self.project_root,
                image=self.config.sandbox_docker_image,
                memory=self.config.sandbox_memory,
                cpus=self.config.sandbox_cpus,
                pids_limit=self.config.sandbox_pids_limit,
            )
            self.tool_registry.register_tool(SandboxBashTool(provider))
            self.tool_registry.restrict_to_tools({"Bash"})
            self.logger.warning(
                "Docker sandbox mode: only container Bash is exposed; "
                "host file, MCP, skill and subagent tools are denied"
            )

        # MCP prompt sync (tools registered by MCPFeature.init)
        self.context_builder.set_mcp_tools_prompt(
            "" if self._sandbox_mode == "docker" else self._mcp_tools_prompt
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

        # Dispatch the on_model_changed event to all features. Errors per
        # feature are swallowed so one reactor's bug doesn't break the switch.
        for feat in getattr(self, "_features", []):
            try:
                feat.on_model_changed(self, previous, model_name)
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.warning(
                    "Feature %s.on_model_changed raised: %s",
                    getattr(feat, "name", type(feat).__name__), exc,
                )

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
        if self._sandbox_mode == "docker":
            raise RuntimeError(
                "Worktree root switching is disabled in Docker sandbox mode; "
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
            self._skill_loader = SkillLoader(self.project_root)
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
        """Request cooperative cancellation, checked between LLM/tool steps.

        An in-flight provider request or shell process cannot be preempted.
        The turn exits before the next tool/LLM step after that call returns.
        """
        self._interrupt_requested.set()

    def run(self, input_text: str, **kwargs) -> str:
        """Run each turn with isolated configuration in the current thread."""
        from core.runtime.config_scope import scoped_config
        with scoped_config(self.config):
            return self._run_scoped(input_text, **kwargs)

    def _run_scoped(self, input_text: str, **kwargs) -> str:
        """Compatibility adapter for isolated Agent turn lifecycle."""
        from core.runtime.turn_lifecycle import AgentTurnLifecycle
        return AgentTurnLifecycle(self).run(input_text, **kwargs)

    def close(self):
        """Close the Agent: run feature cleanups in reverse-init order,
        then finalize framework-level state (trace logger).

        Idempotent: calling close() more than once is a no-op after the
        first invocation. Per-feature exceptions are logged but do not
        abort the chain — every feature gets its chance to clean up
        even if an earlier one fails.

        Cleanup flow (LIFO over self._features by order):
            100 SessionFeature  → save final snapshot
             90 VCRFeature      → (no-op)
             85 HookFeature     → run SessionEnd hooks
             70 BackgroundTask  → (no-op)
             60 PlanMode        → (no-op)
             55 Budget          → (no-op)
             40 DelegateMode    → (no-op)
             30 AgentTeams      → team_manager.shutdown()
             25 MCP             → close all mcp clients
             20 Worktree        → (no-op; ExitWorktree tool handles its own)
            [framework]         → trace_logger.finalize()
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True

        # Reverse-init iteration so teardown mirrors construction (LIFO).
        for feat in reversed(getattr(self, "_features", []) or []):
            try:
                feat.cleanup(self)
            except Exception as exc:
                self.logger.warning(
                    "Feature %s.cleanup failed: %s",
                    getattr(feat, "name", type(feat).__name__), exc,
                )

        # Framework-level teardown (not associated with any feature).
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
        """Collect system prompt blocks from all Features.

        Each Feature contributes zero or more system-level text blocks
        that are injected into the next LLM call without polluting the
        user message history.
        """
        blocks: list[str] = []
        for feat in self._features:
            blocks.extend(feat.runtime_blocks(self, step))
        if blocks and hasattr(self.context_builder, "set_runtime_system_blocks"):
            self.context_builder.set_runtime_system_blocks(blocks)

    def _maybe_compress_history(self, pending_input: str, trace_logger, step: int) -> None:
        from core.runtime.context_runtime import ContextRuntime
        ContextRuntime(self).maybe_compress_history(pending_input, trace_logger, step)

    def _build_step_messages(self, trace_logger, step: int):
        from core.runtime.context_runtime import ContextRuntime
        return ContextRuntime(self).build_step_messages(trace_logger, step)

    def _invoke_llm_with_retry(
        self,
        messages, base_messages, tools_schema, tool_choice,
        show_raw: bool, trace_logger, step: int,
    ) -> dict | None:
        """调用 LLM，含一次空响应重试。返回 None 表示最终空响应。"""
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

            raw_response = ModelGateway(
                self.llm, streaming=bool(getattr(self.config, "llm_streaming", True)),
            ).invoke(
                messages=messages, tools_schema=tools_schema,
                tool_choice=tool_choice, features=self._features,
                agent=self, on_delta=_on_delta,
            )
            if show_raw:
                self.last_response_raw = (
                    raw_response.model_dump()
                    if hasattr(raw_response, "model_dump")
                    else raw_response
                )

            response_text = extract_content(raw_response) or ""
            reasoning_content = extract_reasoning_content(raw_response)
            usage = extract_usage(raw_response)
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
            return None

    def _execute_step_tools(
        self, tool_calls, response_text, reasoning_content, trace_logger, step: int,
    ) -> None:
        """执行当前 step 的所有 tool_calls，写入 assistant + tool 消息到历史。"""
        # ensure each tool_call has an id (OpenAI strict requirement)
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
            trace_logger,
            "assistant",
            assistant_content,
            {"action_type": "tool_call", "tool_calls": tool_calls},
            step,
        )

        for call in tool_calls:
            if self._interrupt_requested.is_set():
                break
            tool_name = call.get("name") or "unknown_tool"
            tool_call_id = call.get("id") or f"call_{uuid.uuid4().hex}"
            raw_args = call.get("arguments") or {}
            tool_input, parse_err = ensure_json_input(raw_args)
            if parse_err:
                error_result = {
                    "status": "error",
                    "error": {"code": "INVALID_PARAM", "message": f"Tool arguments parse error: {parse_err}"},
                    "data": {},
                }
                observation = json.dumps(error_result, ensure_ascii=False)
                trace_logger.log_event(
                    "error",
                    {
                        "stage": "tool_call_parse",
                        "error_code": "INVALID_PARAM",
                        "message": str(parse_err),
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                    },
                    step=step,
                )
            else:
                trace_logger.log_event("tool_call", {"tool": tool_name, "args": tool_input, "tool_call_id": tool_call_id}, step=step)
                self.event_sink.emit(AgentEvent(EventType.TOOL_STARTED, {
                    "tool": tool_name,
                    "input": tool_input,
                    "tool_call_id": tool_call_id,
                }, step=step))
                if self.console_verbose:
                    self._console(f"\n🎬 Action: {tool_name}[{tool_input}]\n")
                elif self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug("Action: %s %s", tool_name, tool_input)
                try:
                    observation = self._execute_tool(tool_name, tool_input)
                    try:
                        result_obj = json.loads(observation)
                        trace_logger.log_event("tool_result", {"tool": tool_name, "result": result_obj}, step=step)
                    except json.JSONDecodeError:
                        trace_logger.log_event("tool_result", {"tool": tool_name, "result": {"text": observation}}, step=step)
                except Exception as e:
                    error_result = {"status": "error", "error": {"code": "EXECUTION_ERROR", "message": str(e)}, "data": {}}
                    observation = json.dumps(error_result, ensure_ascii=False)
                    trace_logger.log_event("error", {"stage": "tool_execution", "error_code": "EXECUTION_ERROR", "message": str(e), "tool": tool_name, "traceback": tb.format_exc()}, step=step)

                # Emit structured event for UI — success or failure.
                try:
                    obs_obj = json.loads(observation)
                    status = obs_obj.get("status", "success")
                except (json.JSONDecodeError, TypeError):
                    status = "success"
                self.event_sink.emit(AgentEvent(EventType.TOOL_COMPLETED, {
                    "tool": tool_name,
                    "tool_call_id": tool_call_id,
                    "status": status,
                    "output": observation,
                    "input": tool_input,
                }, step=step))

            self.history_manager.append_tool(
                tool_name=tool_name,
                raw_result=observation,
                metadata={"step": step, "tool_call_id": tool_call_id},
                project_root=self.project_root,
            )
            self._log_message_write(
                trace_logger,
                "tool",
                observation,
                {"tool_name": tool_name, "tool_call_id": tool_call_id},
                step,
            )

            if self.console_verbose:
                display_obs = observation[:300] + "..." if len(observation) > 300 else observation
                self._console(f"\n👀 Observation: {display_obs}\n")
            elif self.logger.isEnabledFor(logging.DEBUG):
                display_obs = observation[:300] + "..." if len(observation) > 300 else observation
                self.logger.debug("Observation: %s", display_obs)

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
        if self._system_messages_override:
            return [dict(m) for m in self._system_messages_override]
        return self.context_builder.get_system_messages()

    def _build_messages(self, history_messages: list[dict]) -> list[dict]:
        system_messages = self._get_system_messages_for_run()
        return list(system_messages) + list(history_messages)

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
        """Execute one tool via the decoupled feature-aware runtime port."""
        from core.runtime.tool_runtime import ToolRuntime
        if not hasattr(self, "_last_tool_timing"):
            self._last_tool_timing: dict[str, int] = {}
        return ToolRuntime(self.tool_registry, self.logger).execute(
            tool_name,
            tool_input,
            agent=self,
            features=self._features,
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
