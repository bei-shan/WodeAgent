import json
import uuid
import os
import logging
import sys
import traceback as tb
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
from tools.builtin.list_files import ListFilesTool
from tools.builtin.search_files_by_name import SearchFilesByNameTool
from tools.builtin.search_code import GrepTool
from tools.builtin.read_file import ReadTool
from tools.builtin.write_file import WriteTool
from tools.builtin.edit_file import EditTool
from tools.builtin.edit_file_multi import MultiEditTool
from tools.builtin.todo_write import TodoWriteTool
from tools.builtin.skill import SkillTool
from tools.builtin.bash import BashTool
from tools.builtin.ask_user import AskUserTool
from tools.builtin.task import TaskTool
from tools.builtin.enter_worktree import EnterWorktreeTool
from tools.builtin.exit_worktree import ExitWorktreeTool
from tools.builtin.enter_plan_mode import EnterPlanModeTool
from tools.builtin.exit_plan_mode import ExitPlanModeTool
from tools.builtin.task_output import TaskOutputTool
from tools.mcp.loader import register_mcp_servers, format_mcp_tools_prompt
from tools.permission_gate import create_permission_gate
from utils import setup_logger
from core.skills.skill_loader import SkillLoader


class CodeAgent(Agent):
    """
    Code Agent - 基于 ReAct 的代码助手
    
    上下文工程改造（按方案 D3）：
    - 使用 HistoryManager 管理会话历史
    - ReAct 每一步同步写入 assistant/tool 消息到 history
    - 支持压缩触发和 Summary 生成
    """
    DELEGATION_ALLOWED_TOOLS = {
        "TeamCreate",
        "SendMessage",
        "TeamStatus",
        "TeamDelete",
        "TeamCleanup",
        "TeamApprovals",
        "TeamApprovePlan",
        "TeamFanout",
        "TeamCollect",
        "TeamTaskCreate",
        "TeamTaskGet",
        "TeamTaskUpdate",
        "TeamTaskList",
        "TodoWrite",
        "AskUser",
    }

    PLAN_MODE_TOOLS = {
        "Read",
        "Grep",
        "Glob",
        "LS",
        "TodoWrite",
        "TaskOutput",
        "EnterPlanMode",
        "ExitPlanMode",
        "AskUser",
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
        self.max_steps = 50
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

        # Worktree 会话隔离管理器
        worktree_store_dir = os.getenv("WORKTREE_STORE_DIR", ".worktrees")
        worktree_base_ref = os.getenv("WORKTREE_BASE_REF", "fresh")
        self._worktree_manager = WorktreeManager(
            project_root=self._original_project_root,
            store_dir=worktree_store_dir,
            base_ref=worktree_base_ref,
        )
        self._active_worktree: Optional[dict] = None

        # Plan mode state
        self._in_plan_mode = False
        self._plan_text: Optional[str] = None

        # Background task runner
        from core.background_task import BackgroundTaskRunner
        self._background_runner = BackgroundTaskRunner(project_root=self._original_project_root)

        # 创建 Summary 生成器（Phase 7）
        summary_generator = create_summary_generator(
            llm=self.llm,
            config=self.config,
            verbose=self.verbose,
        )
        
        # 历史管理器（替代 Agent._history）
        self.history_manager = HistoryManager(
            config=self.config,
            summary_generator=summary_generator,
        )
        
        # Skills
        self._skill_loader = SkillLoader(self.project_root)
        self._skills_prompt = ""
        self._refresh_skills_prompt()

        # 注册工具
        self._register_builtin_tools()
        self._mcp_clients = []
        self._mcp_tools_prompt = ""
        self._register_mcp_tools()

        # 软沙箱权限注入（工具注册完成后）
        self._permission_gate = create_permission_gate(
            project_root=self.project_root,
        )
        self._inject_permission_gate()
        
        # 上下文构建器
        self.context_builder = ContextBuilder(
            tool_registry=self.tool_registry,
            project_root=self.project_root,
            system_prompt_override=self.system_prompt,
            mcp_tools_prompt=self._mcp_tools_prompt,
            skills_prompt=self._skills_prompt,
        )

        # Trace 日志（单实例贯穿 Agent 生命周期）
        self.trace_logger = create_trace_logger()
        self._system_messages_logged = False
        self._run_id = 0
        self._system_messages_override: Optional[List[dict]] = None
    
    def _register_builtin_tools(self):
        """注册内置工具"""
        self.tool_registry.register_tool(
            ListFilesTool(project_root=self.project_root, working_dir=self.project_root)
        )
        self.tool_registry.register_tool(SearchFilesByNameTool(project_root=self.project_root))
        self.tool_registry.register_tool(GrepTool(project_root=self.project_root))
        self.tool_registry.register_tool(ReadTool(project_root=self.project_root))
        self.tool_registry.register_tool(WriteTool(project_root=self.project_root))
        self.tool_registry.register_tool(EditTool(project_root=self.project_root))
        self.tool_registry.register_tool(MultiEditTool(project_root=self.project_root))
        self.tool_registry.register_tool(TodoWriteTool(project_root=self.project_root))
        self.tool_registry.register_tool(
            SkillTool(project_root=self.project_root, skill_loader=self._skill_loader)
        )
        self.tool_registry.register_tool(BashTool(project_root=self.project_root))
        self.tool_registry.register_tool(
            AskUserTool(project_root=self.project_root, interactive=self.interactive)
        )
        # Task tool for subagent delegation
        self.tool_registry.register_tool(
            TaskTool(
                project_root=self.project_root,
                main_llm=self.llm,
                tool_registry=self.tool_registry,
                team_manager=self.team_manager,
                background_runner=self._background_runner,
            )
        )
        # Worktree session isolation tools
        self.tool_registry.register_tool(
            EnterWorktreeTool(
                project_root=self.project_root,
                worktree_manager=self._worktree_manager,
                code_agent=self,
            )
        )
        self.tool_registry.register_tool(
            ExitWorktreeTool(
                project_root=self.project_root,
                worktree_manager=self._worktree_manager,
                code_agent=self,
            )
        )
        # Plan mode tools
        self.tool_registry.register_tool(
            EnterPlanModeTool(project_root=self.project_root, code_agent=self)
        )
        self.tool_registry.register_tool(
            ExitPlanModeTool(project_root=self.project_root, code_agent=self)
        )
        # Background task output
        self.tool_registry.register_tool(
            TaskOutputTool(project_root=self.project_root, background_runner=self._background_runner)
        )
        if self.enable_agent_teams:
            self._register_agent_teams_tools()

    def _register_agent_teams_tools(self) -> None:
        try:
            from tools.builtin.team_create import TeamCreateTool
            from tools.builtin.send_message import SendMessageTool
            from tools.builtin.team_status import TeamStatusTool
            from tools.builtin.team_delete import TeamDeleteTool
            from tools.builtin.team_cleanup import TeamCleanupTool
            from tools.builtin.team_approvals import TeamApprovalsTool
            from tools.builtin.team_approve_plan import TeamApprovePlanTool
            from tools.builtin.team_fanout import TeamFanoutTool
            from tools.builtin.team_collect import TeamCollectTool
            from tools.builtin.team_task_create import TeamTaskCreateTool
            from tools.builtin.team_task_get import TeamTaskGetTool
            from tools.builtin.team_task_update import TeamTaskUpdateTool
            from tools.builtin.team_task_list import TeamTaskListTool
            from tools.builtin.team_list import TeamListTool
            from tools.builtin.team_retry import TeamRetryTool
        except Exception as exc:
            self.logger.warning("AgentTeams enabled but team tools unavailable: %s", exc)
            return

        self.tool_registry.register_tool(TeamCreateTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(SendMessageTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamStatusTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamDeleteTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamCleanupTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamApprovalsTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamApprovePlanTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamFanoutTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamCollectTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamTaskCreateTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamTaskGetTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamTaskUpdateTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamTaskListTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamListTool(project_root=self.project_root, team_manager=self.team_manager))
        self.tool_registry.register_tool(TeamRetryTool(project_root=self.project_root, team_manager=self.team_manager))

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
        """Exit plan mode, restore full tools, inject plan into context."""
        self._in_plan_mode = False
        self._plan_text = plan.strip()

        # Append a TodoWrite reminder so the LLM tracks plan progress.
        steps = self._extract_plan_steps(plan)
        if steps:
            todo_lines = ["Use TodoWrite to track the plan:"]
            for i, step in enumerate(steps, 1):
                todo_lines.append(f"  {i}. [pending] {step}")
            self._plan_text += "\n\n" + "\n".join(todo_lines)

        self.logger.info("Exited plan mode, plan length=%d, steps=%d", len(self._plan_text), len(steps))

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
        return response_text

    def close(self):
        """关闭 Agent 并写入 trace 总结"""
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
            tools_schema = self._get_openai_tools_for_current_mode()
            runtime_blocks: list[str] = []

            # AgentTeams runtime state
            if self.enable_agent_teams and self.team_manager and hasattr(self.context_builder, "set_runtime_system_blocks"):
                events = self.team_manager.drain_events()
                runtime_state = self.team_manager.export_state()
                runtime_blocks.extend(self._format_runtime_system_blocks(events, runtime_state=runtime_state))

            # Background task summary
            bg_summary = self._background_runner.summary_text()
            if bg_summary:
                runtime_blocks.append(bg_summary)

            # Plan text injection
            if self._plan_text:
                runtime_blocks.append(f"[Plan]\n{self._plan_text}")
                self._plan_text = None  # only inject once

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
            raw_response = self.llm.invoke_raw(messages, tools=tools_schema, tool_choice=tool_choice)
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

    def save_session(self, path: str) -> None:
        """保存会话快照（含 system messages）。"""
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
            code_law_text=self.context_builder._cached_code_law,
            skills_prompt=self._skills_prompt,
            mcp_tools_prompt=self._mcp_tools_prompt,
            read_cache=self.tool_registry.export_read_cache(),
            tool_output_dir="tool-output",
            schema_version=1,
            teams_snapshot=teams_snapshot,
            parallel_work_index=(teams_snapshot.get("work_items", {}) if isinstance(teams_snapshot, dict) else {}),
            team_store_dir=self.team_store_dir,
            task_store_dir=self.task_store_dir,
            worktree_state=worktree_state,
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
        has_events = bool(events)
        state = runtime_state if isinstance(runtime_state, dict) else {}
        teams = state.get("teams") if isinstance(state.get("teams"), dict) else {}
        work_items = state.get("work_items") if isinstance(state.get("work_items"), dict) else {}
        approvals = state.get("approvals") if isinstance(state.get("approvals"), dict) else {}
        task_board = state.get("task_board") if isinstance(state.get("task_board"), dict) else {}
        if not has_events and not work_items:
            return []
        lines = ["[Team Runtime]"]

        for team_name in sorted(work_items.keys()):
            counts = work_items.get(team_name)
            if not isinstance(counts, dict):
                continue
            queued = int(counts.get("queued", 0) or 0)
            running = int(counts.get("running", 0) or 0)
            succeeded = int(counts.get("succeeded", 0) or 0)
            failed = int(counts.get("failed", 0) or 0)
            lines.append(
                f"- {team_name} work queued={queued} running={running} succeeded={succeeded} failed={failed}"
            )
            team_state = teams.get(team_name) if isinstance(teams, dict) else {}
            if isinstance(team_state, dict):
                idle_members = team_state.get("idle_teammates")
                active_members = team_state.get("active_teammates")
                if isinstance(idle_members, list) or isinstance(active_members, list):
                    idle_count = len(idle_members) if isinstance(idle_members, list) else 0
                    active_count = len(active_members) if isinstance(active_members, list) else 0
                    lines.append(f"- {team_name} teammates active={active_count} idle={idle_count}")
                last_error = str(team_state.get("last_error") or "").strip()
                if last_error:
                    compact_error = " ".join(last_error.split())
                    if len(compact_error) > 120:
                        compact_error = f"{compact_error[:117]}..."
                    lines.append(f"- {team_name} last_error={compact_error}")
            approval_counts = approvals.get(team_name) if isinstance(approvals, dict) else {}
            if isinstance(approval_counts, dict):
                pending = int(approval_counts.get("pending", 0) or 0)
                approved = int(approval_counts.get("approved", 0) or 0)
                rejected = int(approval_counts.get("rejected", 0) or 0)
                if pending or approved or rejected:
                    lines.append(
                        f"- {team_name} approvals pending={pending} approved={approved} rejected={rejected}"
                    )
            board_counts = task_board.get(team_name) if isinstance(task_board, dict) else {}
            if isinstance(board_counts, dict):
                blocked = int(board_counts.get("blocked", 0) or 0)
                pending_tasks = int(board_counts.get("pending", 0) or 0)
                in_progress = int(board_counts.get("in_progress", 0) or 0)
                if blocked or pending_tasks or in_progress:
                    lines.append(
                        f"- {team_name} tasks blocked={blocked} pending={pending_tasks} in_progress={in_progress}"
                    )

        deduped_events: list[dict] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for event in events:
            if not isinstance(event, dict):
                continue
            team = str(event.get("team", "unknown"))
            event_type = str(event.get("type", "event"))
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            signature = (
                team,
                event_type,
                str(payload.get("message_id") or ""),
                str(payload.get("work_id") or ""),
                str(payload.get("status") or ""),
            )
            if signature in seen:
                continue
            seen.add(signature)
            deduped_events.append(event)

        max_event_lines = 8
        for event in deduped_events[:max_event_lines]:
            team = event.get("team", "unknown")
            event_type = event.get("type", "event")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            status = payload.get("status")
            message_id = payload.get("message_id")
            work_id = payload.get("work_id")
            if work_id and status:
                lines.append(f"- event {team}:{event_type} work={work_id} status={status}")
            elif message_id and status:
                lines.append(f"- event {team}:{event_type} message={message_id} status={status}")
            elif message_id:
                lines.append(f"- event {team}:{event_type} message={message_id}")
            else:
                lines.append(f"- event {team}:{event_type}")
        if len(deduped_events) > max_event_lines:
            lines.append(f"- ... {len(deduped_events) - max_event_lines} more events")

        limit = max(2, int(max_lines or 0))
        if len(lines) > limit:
            hidden = len(lines) - (limit - 1)
            lines = lines[: limit - 1] + [f"- ... {hidden} more lines"]

        return ["\n".join(lines)]

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
        res = self.tool_registry.execute_tool(tool_name, tool_input)
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
