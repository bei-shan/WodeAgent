import json
import uuid
import os
import logging
import sys
import traceback as tb
from pathlib import Path
from typing import Any, Optional, List, Tuple

from core.agent import Agent
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
from core.context_engine.input_preprocessor import preprocess_input
from core.context_engine.summary_compressor import create_summary_generator
from core.session_store import build_session_snapshot, save_session_snapshot, load_session_snapshot
from core.team_engine.display_mode import resolve_teammate_mode
from tools.registry import ToolRegistry
from core.worktree.manager import WorktreeManager, WorktreeError
from core.features import collect_all_features
from core.features.base import AgentFeature
from tools.mcp.loader import register_mcp_servers, format_mcp_tools_prompt
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
    ):
        super().__init__(name, llm, system_prompt=system_prompt, config=config)
        self.project_root = project_root
        self._original_project_root = project_root
        self.tool_registry = tool_registry
        self.logger = logger or setup_logger(
            name=f"agent.{self.name}",
            level=self.config.log_level,
        )
        self.last_response_raw: Optional[Any] = None
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
        self.team_manager = None
        if self.enable_agent_teams:
            try:
                from core.team_engine.manager import TeamManager
                self.team_manager = TeamManager(
                    project_root=self.project_root,
                    team_store_dir=self.team_store_dir,
                    task_store_dir=self.task_store_dir,
                    llm=self.llm,
                    tool_registry=self.tool_registry,
                    teammate_runtime_mode=self.teammate_runtime_mode,
                )
            except Exception as exc:
                self.logger.warning("Failed to initialize TeamManager, AgentTeams disabled: %s", exc)
                self.enable_agent_teams = False
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
        # Inject config into modules that use module-level config singletons
        self._inject_config_to_modules()

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

        # Register dependency providers — tools receive these via constructor injection
        bootstrap.provide("code_agent", self)
        bootstrap.provide("team_manager", self.team_manager)
        bootstrap.provide("background_runner", self._background_runner)
        bootstrap.provide("skill_loader", self._skill_loader)
        bootstrap.provide("main_llm", self.llm)
        bootstrap.provide("tool_registry", self.tool_registry)
        bootstrap.provide("interactive", self.interactive)
        bootstrap.provide("worktree_manager", self._worktree_manager)

        # Auto-discover and register all non-team built-in tools
        registered = bootstrap.discover_and_register()
        self.logger.debug("Auto-registered %d built-in tools: %s", len(registered), registered)

        # Team tools (only when AgentTeams is enabled)
        if self.enable_agent_teams and self.team_manager:
            team_registered = register_team_tools(bootstrap)
            self.logger.debug("Registered %d team tools: %s", len(team_registered), team_registered)

        # MCP tools
        self._register_mcp_tools()
        self.context_builder.set_mcp_tools_prompt(self._mcp_tools_prompt)

    def _inject_config_to_modules(self) -> None:
        """Inject Config into modules that use module-level singletons.

        This centralises config access — modules check the injected config first,
        then fall back to os.getenv() for backward compatibility.
        """
        from core.context_engine.observation_truncator import set_truncator_config
        from tools.registry import set_registry_config
        from tools.builtin.task import set_task_config

        set_truncator_config(self.config)
        set_registry_config(self.config)
        set_task_config(self.config)

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
        self.project_root = str(wt_path)

        # Refresh tools with new project_root.
        self._inject_permission_gate()
        self.context_builder.project_root = self.project_root if hasattr(self.context_builder, "project_root") else self.context_builder._project_root

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
        self.project_root = str(self._original_project_root)
        self._active_worktree = None

        # Refresh tools with original project_root.
        self._inject_permission_gate()
        if hasattr(self.context_builder, "project_root"):
            self.context_builder.project_root = self.project_root

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

    def _register_mcp_tools(self) -> None:
        """可选：注册 MCP 工具（基于 MCP_SERVERS 配置）"""
        try:
            clients, tools_meta = register_mcp_servers(self.tool_registry, self.project_root)
            self._mcp_clients = clients
            self._mcp_tools_prompt = format_mcp_tools_prompt(tools_meta)
            if tools_meta:
                self.logger.info("MCP tools loaded: %d", len(tools_meta))
                if self.logger.isEnabledFor(logging.DEBUG):
                    for tool in tools_meta:
                        name = tool.get("name") or ""
                        description = (tool.get("description") or "").strip()
                        if description:
                            self.logger.debug("MCP tool: %s - %s", name, description)
                        else:
                            self.logger.debug("MCP tool: %s", name)
        except Exception as exc:
            if self.logger:
                self.logger.warning("MCP registration skipped: %s", exc)

    def _retry_pending_mcp_servers(self) -> None:
        """Retry connecting any MCP servers that failed during background init.

        Called at the start of each ReAct step so that tools appear as
        soon as the server becomes reachable — no manual trigger needed.
        """
        try:
            from tools.mcp.loader import get_pending_server_names, retry_pending_server
            pending = get_pending_server_names()
            if not pending:
                return
            for name in pending:
                ok = retry_pending_server(
                    self.tool_registry, name, timeout=10.0,
                )
                if ok:
                    self.logger.info("MCP server '%s' recovered, tools now available", name)
                    # Rebuild MCP tools prompt so LLM sees the new tools.
                    all_tools = self.tool_registry.get_all_tools()
                    mcp_tools_meta = [
                        {"name": t.name, "description": getattr(t, "description", "")}
                        for t in all_tools
                        if getattr(t, "name", "").startswith("mcp__")
                    ]
                    self._mcp_tools_prompt = format_mcp_tools_prompt(mcp_tools_meta)
        except Exception:
            pass

    def _inject_permission_gate(self) -> None:
        """将 PermissionGate 注入到所有已注册的工具实例中。

        对于已有 permission_gate 属性的工具（如子代理复用），保留原值。
        """
        for tool in self.tool_registry.get_all_tools():
            if getattr(tool, "_permission_gate", None) is None:
                tool._permission_gate = self._permission_gate

    def run(self, input_text: str, **kwargs) -> str:
        """
        Code Agent 的入口（Message List 自然累积模式）
        
        流程：
        1. 预处理用户输入（@file 解析）
        2. 检查是否需要压缩历史
        3. 将用户消息写入 history（轮次开始）
        4. 运行 ReAct 循环（每步 assistant/tool 消息自然累积）
        5. 返回最终结果
        
        Message List 模式：
        - 不再使用 scratchpad 拼接
        - 每步的 messages 由 history 自然累积
        - L1/L2 作为 system messages
        - L3 是累积的 user/assistant/tool
        """
        show_raw = kwargs.pop("show_raw", False)
        if not show_raw:
            self.last_response_raw = None

        if self.console_progress:
            self._console("⏳ Agent 正在处理，请稍候...")

        # 1. 预处理用户输入（@file 解析）
        self._refresh_skills_prompt()
        self.context_builder.set_skills_prompt(self._skills_prompt)
        preprocess_result = preprocess_input(input_text)
        processed_input = preprocess_result.processed_input

        # 2. Parse token budget from user input
        if hasattr(self, "_budget_tracker"):
            self._budget_tracker.parse_from_input(input_text)

        if preprocess_result.mentioned_files:
            mentioned = ", ".join(preprocess_result.mentioned_files)
            if self.console_verbose:
                self._console(f"\n📎 检测到文件引用: {mentioned}")
                if preprocess_result.truncated_count > 0:
                    self._console(f"   (另有 {preprocess_result.truncated_count} 个文件被省略)")
            elif self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("检测到文件引用: %s", mentioned)
                if preprocess_result.truncated_count > 0:
                    self.logger.debug("另有 %d 个文件被省略", preprocess_result.truncated_count)

        trace_logger = self.trace_logger
        self._run_id += 1
        run_id = self._run_id

        self._log_system_messages_if_needed(trace_logger)
        trace_logger.log_event(
            "run_start",
            {
                "run_id": run_id,
                "input": input_text,
                "processed": processed_input,
            },
            step=0,
        )
        
        # 2. 压缩检测改为每次 ReAct 之前（循环内）

        # 3. 将用户消息写入 history（轮次开始时写入）
        self.history_manager.append_user(processed_input)
        trace_logger.log_event("user_input", {"text": input_text, "processed": processed_input}, step=0)
        self._log_message_write(trace_logger, "user", processed_input, {}, step=0)

        if self.console_verbose:
            self._console(f"\n⚙️ Engine 启动: {input_text}")
        elif self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug("Engine 启动: %s", input_text)

        response_text = ""
        try:
            response_text = self._react_loop(
                pending_input=processed_input,
                show_raw=show_raw,
                trace_logger=trace_logger,
            )
        finally:
            trace_logger.log_event(
                "run_end",
                {"run_id": run_id, "final": response_text if "response_text" in locals() else ""},
                step=0,
            )
        if self.console_progress:
            self._console("✅ Agent 已完成")

        self.logger.debug("response=%s", response_text)
        self.logger.info("history_size=%d, rounds=%d",
                        self.history_manager.get_message_count(),
                        self.history_manager.get_rounds_count())

        # 自动保存当前对话
        self._auto_save_session()

        return response_text

    def close(self):
        """关闭 Agent 并写入 trace 总结"""
        # Final auto-save
        try:
            self._auto_save_session()
        except Exception:
            pass

        # SessionEnd hooks
        if self._hook_manager.has_any_hooks:
            try:
                self._hook_manager.run_session_end()
            except Exception as exc:
                self.logger.warning("SessionEnd hooks failed: %s", exc)

        if self.trace_logger:
            self.trace_logger.finalize()
            self.trace_logger = None
        for client in getattr(self, "_mcp_clients", []):
            try:
                client.close_sync()
            except Exception:
                pass

    # =========================================================================
    # ReAct Core（Message List 自然累积模式）
    # =========================================================================

    # ------------------------------------------------------------------
    # ReAct 循环（拆分为 3 个子方法以提高可读性和可测试性）
    # ------------------------------------------------------------------

    def _react_loop(
        self,
        pending_input: str,
        show_raw: bool,
        trace_logger,
    ) -> str:
        """ReAct 循环入口。每步：准备 → LLM → 工具执行 → 下一轮。"""
        tool_choice = "auto"

        for step in range(1, self.max_steps + 1):
            # Auto-retry pending MCP servers each step so tools appear
            # as soon as the server becomes reachable.
            self._retry_pending_mcp_servers()

            tools_schema = self._get_openai_tools_for_current_mode()
            runtime_blocks: list[str] = []

            # Collect runtime blocks from all features
            for feat in self._features:
                runtime_blocks.extend(feat.runtime_blocks(self, step))

            if runtime_blocks and hasattr(self.context_builder, "set_runtime_system_blocks"):
                self.context_builder.set_runtime_system_blocks(runtime_blocks)

            if self.console_verbose:
                self._console(f"\n--- Step {step}/{self.max_steps} ---")
            elif self.console_progress:
                self._console(f"… Step {step}/{self.max_steps}")
            elif self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("Step %d/%d", step, self.max_steps)

            self._maybe_compress_history(pending_input, trace_logger, step)

            messages, base_messages = self._build_step_messages(trace_logger, step)

            result = self._invoke_llm_with_retry(
                messages, base_messages, tools_schema, tool_choice,
                show_raw, trace_logger, step,
            )
            if result is None:
                break

            tool_calls = result["tool_calls"]
            response_text = result["response_text"]
            reasoning_content = result["reasoning_content"]

            if not tool_calls and (not response_text or not str(response_text).strip()):
                break

            if tool_calls:
                self._execute_step_tools(tool_calls, response_text, reasoning_content, trace_logger, step)
                continue

            # 无工具调用：视为最终回答
            final_text = str(response_text).strip()
            self.history_manager.append_assistant(
                content=final_text,
                metadata={"step": step, "action_type": "final"},
                reasoning_content=reasoning_content,
            )
            self._log_message_write(trace_logger, "assistant", final_text, {"action_type": "final"}, step)
            trace_logger.log_event("finish", {"final": final_text}, step=step)
            return final_text

        return "抱歉，我无法在限定步数内完成这个任务。"

    # ------------------------------------------------------------------
    # ReAct 子方法
    # ------------------------------------------------------------------

    def _maybe_compress_history(self, pending_input: str, trace_logger, step: int) -> None:
        """检查并在需要时压缩历史。"""
        if not self.history_manager.should_compress(pending_input):
            return

        estimated_tokens = self.history_manager.estimate_context_tokens(pending_input)
        threshold = int(self.config.context_window * self.config.compression_threshold)
        trace_logger.log_event("history_compression_triggered", {
            "estimated_tokens": estimated_tokens,
            "threshold": threshold,
            "total_usage_tokens": self.history_manager.get_total_usage_tokens(),
            "message_count": self.history_manager.get_message_count(),
        }, step=step)

        if self.console_verbose:
            self._console("\n📦 触发历史压缩...")
        elif self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug("触发历史压缩")

        rounds_before = self.history_manager.get_rounds_count()
        messages_before = self.history_manager.get_message_count()

        compress_info = self.history_manager.compact(
            on_event=lambda ev, payload: trace_logger.log_event(ev, payload, step=step),
            return_info=True,
        )
        compressed = bool(compress_info.get("compressed"))

        if compressed:
            rounds_after = self.history_manager.get_rounds_count()
            messages_after = self.history_manager.get_message_count()

            trace_logger.log_event("history_compression_completed", {
                "rounds_before": rounds_before,
                "rounds_after": rounds_after,
                "messages_compressed": messages_before - messages_after,
                "summary_generated": compress_info.get("summary_generated", False),
                "details": compress_info,
            }, step=step)

            compressed_history = self.history_manager.to_messages()
            final_context = self.context_builder.build_messages(compressed_history)
            trace_logger.log_event(
                "history_compression_final_context",
                {"message_count": len(final_context), "messages": final_context},
                step=step,
            )

            if self.console_verbose:
                self._console(f"✅ 压缩完成，当前轮次数: {rounds_after}")
                self._print_context_preview(final_context)
            elif self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("压缩完成，当前轮次数: %d", rounds_after)
                self._print_context_preview(final_context)

    def _build_step_messages(self, trace_logger, step: int):
        """构建当前 step 的 messages 列表，返回 (messages, base_messages)。"""
        history_messages = self.history_manager.to_messages()
        messages = self._build_messages(history_messages)
        base_messages = messages
        trace_logger.log_event(
            "context_build",
            {"message_count": len(messages), "history_count": len(history_messages)},
            step=step,
        )
        return messages, base_messages

    def _invoke_llm_with_retry(
        self,
        messages, base_messages, tools_schema, tool_choice,
        show_raw: bool, trace_logger, step: int,
    ) -> dict | None:
        """调用 LLM，含一次空响应重试。返回 None 表示最终空响应。"""
        empty_retry_used = False
        reasoning_content = None

        while True:
            # Build intercept chain (features wrap the real LLM call).
            def _real_call():
                return self.llm.invoke_raw(
                    messages, tools=tools_schema, tool_choice=tool_choice
                )
            intercept = _real_call
            for feat in reversed(self._features):
                prev = intercept
                intercept = lambda f=feat, p=prev: f.llm_intercept(
                    self, messages, tools_schema, tool_choice, p
                )
            raw_response = intercept()
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
        """Build a session snapshot dict for the current state."""
        system_messages = self._get_system_messages_for_run()
        history_messages = self.history_manager.serialize_messages()
        tool_schema = self._get_openai_tools_for_current_mode()
        teams_snapshot = self.team_manager.export_state() if self.team_manager else {}
        worktree_state = None
        if self._active_worktree:
            worktree_state = {
                "name": self._active_worktree.get("name"),
                "path": self._active_worktree.get("path"),
                "branch": self._active_worktree.get("branch"),
            }
        return build_session_snapshot(
            system_messages=system_messages,
            history_messages=history_messages,
            tool_schema=tool_schema,
            project_root=self.project_root,
            cwd=".",
            code_law_text=self.context_builder._load_code_law(),
            skills_prompt=self._skills_prompt,
            mcp_tools_prompt=self._mcp_tools_prompt,
            read_cache=self.tool_registry.export_read_cache(),
            tool_output_dir="tool-output",
            schema_version=2,
            teams_snapshot=teams_snapshot,
            parallel_work_index=(teams_snapshot.get("work_items", {}) if isinstance(teams_snapshot, dict) else {}),
            team_store_dir=self.team_store_dir,
            task_store_dir=self.task_store_dir,
            worktree_state=worktree_state,
            # ── v2 tree fields ──
            cursor_id=self.history_manager.get_cursor_id(),
            history_entries=self.history_manager.serialize_entries(),
            labels=self.history_manager._labels,
            current_model=self.history_manager.get_current_model(),
            thinking_level=self.history_manager.get_thinking_level(),
        )

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
        """保存会话快照（含 system messages）。(legacy — uses auto-save now)"""
        system_messages = self._get_system_messages_for_run()
        history_messages = self.history_manager.serialize_messages()
        tool_schema = self._get_openai_tools_for_current_mode()
        teams_snapshot = self.team_manager.export_state() if self.team_manager else {}
        worktree_state = None
        if self._active_worktree:
            worktree_state = {
                "name": self._active_worktree.get("name"),
                "path": self._active_worktree.get("path"),
                "branch": self._active_worktree.get("branch"),
            }
        snapshot = build_session_snapshot(
            system_messages=system_messages,
            history_messages=history_messages,
            tool_schema=tool_schema,
            project_root=self.project_root,
            cwd=".",
            code_law_text=self.context_builder._load_code_law(),
            skills_prompt=self._skills_prompt,
            mcp_tools_prompt=self._mcp_tools_prompt,
            read_cache=self.tool_registry.export_read_cache(),
            tool_output_dir="tool-output",
            schema_version=2,
            teams_snapshot=teams_snapshot,
            parallel_work_index=(teams_snapshot.get("work_items", {}) if isinstance(teams_snapshot, dict) else {}),
            team_store_dir=self.team_store_dir,
            task_store_dir=self.task_store_dir,
            worktree_state=worktree_state,
            cursor_id=self.history_manager.get_cursor_id(),
            history_entries=self.history_manager.serialize_entries(),
            labels=self.history_manager._labels,
            current_model=self.history_manager.get_current_model(),
            thinking_level=self.history_manager.get_thinking_level(),
        )
        save_session_snapshot(path, snapshot)

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
        if not self._is_tool_allowed_in_delegate_mode(tool_name):
            payload = {
                "status": "error",
                "data": {},
                "text": f"Tool '{tool_name}' is blocked in delegate mode.",
                "error": {
                    "code": "PERMISSION_DENIED",
                    "message": f"Tool '{tool_name}' is not allowed in delegate mode.",
                },
                "stats": {"time_ms": 0},
                "context": {"cwd": ".", "params_input": tool_input if isinstance(tool_input, dict) else {"input": tool_input}},
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)

        # PreToolUse interception (features)
        normalized_input = tool_input if isinstance(tool_input, dict) else {"input": tool_input}
        for feat in self._features:
            pre_result = feat.pre_tool_use(self, tool_name, normalized_input)
            if pre_result and pre_result.get("blocked"):
                self.logger.info(
                    "Tool %s blocked by %s: %s",
                    tool_name, feat.name, pre_result.get("reason", "unknown")
                )
                payload = {
                    "status": "error",
                    "data": {},
                    "error": {
                        "code": "HOOK_BLOCKED",
                        "message": pre_result.get("reason", "Blocked"),
                    },
                    "stats": {"time_ms": 0},
                }
                return json.dumps(payload, ensure_ascii=False)
            if pre_result and pre_result.get("system_messages"):
                self._hook_system_messages.extend(pre_result["system_messages"])
            if pre_result and pre_result.get("updated_input"):
                normalized_input = {**normalized_input, **pre_result["updated_input"]}

        res = self.tool_registry.execute_tool(tool_name, normalized_input)

        # PostToolUse interception (features)
        for feat in self._features:
            post_msgs = feat.post_tool_use(self, tool_name, normalized_input, str(res))
            if post_msgs:
                self._hook_system_messages.extend(post_msgs)

        return str(res)

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
