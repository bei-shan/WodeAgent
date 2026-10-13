"""ReAct step orchestrator: an application-neutral AgentLoop.

The host implements AgentLoopPort and is solely responsible for model, tools,
context, events and persistence. AgentLoop owns step ordering and terminal
decision-making. It never imports CodeAgent or a presentation layer.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from core.events import AgentEvent, EventType


class AgentLoopPort(Protocol):
    max_steps: int
    event_sink: Any
    history_manager: Any
    logger: logging.Logger
    console_verbose: bool
    console_progress: bool
    _interrupt_requested: Any
    _run_limit_hit: bool

    def _get_openai_tools_for_current_mode(self) -> list[dict]: ...
    def _collect_runtime_blocks(self, step: int) -> None: ...
    def _maybe_compress_history(self, pending_input: str, trace_logger: Any, step: int) -> None: ...
    def _build_step_messages(self, trace_logger: Any, step: int) -> tuple: ...
    def _invoke_llm_with_retry(self, messages: Any, base_messages: Any, tools_schema: Any,
                               tool_choice: Any, show_raw: bool, trace_logger: Any,
                               step: int) -> Any: ...
    def _execute_step_tools(self, tool_calls: Any, response_text: Any,
                            reasoning_content: Any, trace_logger: Any, step: int) -> None: ...
    def _console(self, text: str) -> None: ...
    def _log_message_write(self, trace_logger: Any, role: str, content: str,
                           metadata: dict, step: int) -> None: ...


class AgentLoop:
    def __init__(self, host: AgentLoopPort):
        self.host = host

    def run(
        self,
        pending_input: str,
        show_raw: bool,
        trace_logger,
    ) -> str:
        """ReAct 循环入口。每步：准备 → LLM → 工具执行 → 下一轮。"""
        host = self.host
        tool_choice = "auto"

        for step in range(1, host.max_steps + 1):
            if host._interrupt_requested.is_set():
                return "已停止生成（用户中断）"
            # Auto-retry pending MCP servers each step so tools appear
            # MCP retry handled by MCPFeature.runtime_blocks()

            host.event_sink.emit(AgentEvent(EventType.STEP_STARTED, {
                "step": step,
                "max_steps": host.max_steps,
            }, step=step))

            tools_schema = host._get_openai_tools_for_current_mode()
            host._collect_runtime_blocks(step)

            if host.console_verbose:
                host._console(f"\n--- Step {step}/{host.max_steps} ---")
            elif host.console_progress:
                host._console(f"… Step {step}/{host.max_steps}")
            elif host.logger.isEnabledFor(logging.DEBUG):
                host.logger.debug("Step %d/%d", step, host.max_steps)

            host._maybe_compress_history(pending_input, trace_logger, step)

            messages, base_messages = host._build_step_messages(trace_logger, step)

            result = host._invoke_llm_with_retry(
                messages, base_messages, tools_schema, tool_choice,
                show_raw, trace_logger, step,
            )
            if host._interrupt_requested.is_set():
                return "已停止生成（用户中断）"
            if result is None:
                break

            tool_calls = result["tool_calls"]
            response_text = result["response_text"]
            reasoning_content = result["reasoning_content"]

            if not tool_calls and (not response_text or not str(response_text).strip()):
                break

            if tool_calls:
                host._execute_step_tools(tool_calls, response_text, reasoning_content, trace_logger, step)
                continue

            # 无工具调用：视为最终回答
            final_text = str(response_text).strip()
            host.event_sink.emit(AgentEvent(EventType.ASSISTANT_FINAL, {
                "content": final_text,
                "step": step,
            }, step=step))
            host.history_manager.append_assistant(
                content=final_text,
                metadata={"step": step, "action_type": "final"},
                reasoning_content=reasoning_content,
            )
            host._log_message_write(trace_logger, "assistant", final_text, {"action_type": "final"}, step)
            trace_logger.log_event("finish", {"final": final_text}, step=step)
            return final_text

        host._run_limit_hit = True
        return "抱歉，我无法在限定步数内完成这个任务。"

