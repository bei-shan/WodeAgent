"""Agent turn transaction boundary: input, tracing, status, saving and events.

The ReAct loop owns steps; this host-neutral lifecycle owns each turn's
begin/end and guarantees an end-event even when model calls fail.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol
from core.events import AgentEvent, EventType
from core.context_engine.input_preprocessor import preprocess_input


class TurnHost(Protocol):
    logger: logging.Logger
    trace_logger: Any
    context_builder: Any
    history_manager: Any
    event_sink: Any
    _run_id: int
    _run_limit_hit: bool

    def _refresh_skills_prompt(self) -> None: ...
    def _log_system_messages_if_needed(self, trace_logger: Any) -> None: ...
    def _log_message_write(self, trace_logger: Any, role: str, content: str,
                           metadata: dict, step: int) -> None: ...
    def _react_loop(self, pending_input: str, show_raw: bool, trace_logger: Any) -> str: ...
    def _auto_save_session(self) -> None: ...
    def _console(self, message: str) -> None: ...


class AgentTurnLifecycle:
    def __init__(self, host: TurnHost):
        self.host = host

    def run(self, input_text: str, **kwargs) -> str:
        """Run one turn with one optional trace root per invocation."""
        from core.runtime.telemetry import get_telemetry

        # Deliberately omit the input, session identifier, paths and output.
        with get_telemetry().span("agent.turn", kind="agent") as observation:
            result = self._run(input_text, **kwargs)
            observation.attribute("ridge.turn.status", getattr(self.host, "last_turn_status", "completed"))
        return self._drive_goal(result)

    def _drive_goal(self, result: str) -> str:
        """Run automatic continuation rounds while an active goal remains.

        Each continuation is a normal turn, so events, saving and status are
        identical to user-triggered ones. The driver owns every stopping rule;
        this loop only counts the rounds it has already run in this invocation.
        """
        driver = getattr(self.host, "_goal_driver", None)
        if driver is None:
            return result
        session_id = getattr(self.host, "_session_id", "")
        rounds = 0
        while True:
            interrupt = getattr(self.host, "_interrupt_requested", None)
            decision = driver.decide(
                session_id,
                turn_status=getattr(self.host, "last_turn_status", "completed"),
                interrupted=bool(interrupt is not None and interrupt.is_set()),
                auto_rounds=rounds,
            )
            if not decision.continue_run:
                return result
            rounds += 1
            result = self._run(decision.prompt, show_raw=False)

    def _run(self, input_text: str, **kwargs) -> str:
        """Run one Agent turn with deterministic completion."""
        host = self.host
        show_raw = kwargs.pop("show_raw", False)
        if not show_raw:
            host.last_response_raw = None

        if host.console_progress:
            host._console("⏳ Agent 正在处理，请稍候...")

        # 1. 预处理用户输入（@file 解析）
        host._refresh_skills_prompt()
        host.context_builder.set_skills_prompt(host._skills_prompt)
        preprocess_result = preprocess_input(input_text)
        processed_input = preprocess_result.processed_input
        memory_refresh = getattr(host, "_refresh_memory_prompt", None)
        if callable(memory_refresh):
            memory_refresh(processed_input)

        # 2. Parse token budget from user input
        if hasattr(host, "_budget_tracker"):
            host._budget_tracker.parse_from_input(input_text)

        if preprocess_result.mentioned_files:
            mentioned = ", ".join(preprocess_result.mentioned_files)
            if host.console_verbose:
                host._console(f"\n📎 检测到文件引用: {mentioned}")
                if preprocess_result.truncated_count > 0:
                    host._console(f"   (另有 {preprocess_result.truncated_count} 个文件被省略)")
            elif host.logger.isEnabledFor(logging.DEBUG):
                host.logger.debug("检测到文件引用: %s", mentioned)
                if preprocess_result.truncated_count > 0:
                    host.logger.debug("另有 %d 个文件被省略", preprocess_result.truncated_count)

        trace_logger = host.trace_logger
        host._run_id += 1
        run_id = host._run_id

        host.event_sink.emit(AgentEvent(EventType.RUN_STARTED, {
            "run_id": run_id,
            "input": input_text,
        }))

        host._log_system_messages_if_needed(trace_logger)
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
        host.history_manager.append_user(processed_input)
        trace_logger.log_event("user_input", {"text": input_text, "processed": processed_input}, step=0)
        host._log_message_write(trace_logger, "user", processed_input, {}, step=0)

        if host.console_verbose:
            host._console(f"\n⚙️ Engine 启动: {input_text}")
        elif host.logger.isEnabledFor(logging.DEBUG):
            host.logger.debug("Engine 启动: %s", input_text)

        response_text = ""
        host._run_limit_hit = False
        host.last_turn_error_kind = None
        run_failed = False
        try:
            response_text = host._react_loop(
                pending_input=processed_input,
                show_raw=show_raw,
                trace_logger=trace_logger,
            )
        except Exception as exc:
            run_failed = True
            host.last_turn_error_kind = getattr(exc, "kind", None)
            raise
        finally:
            stopped = host._interrupt_requested.is_set()
            host.last_turn_status = (
                "cancelled" if stopped else "failed"
                if run_failed or host._run_limit_hit else "completed"
            )
            if host._run_limit_hit:
                host.last_turn_error_kind = "step_limit"
            host._interrupt_requested.clear()
            # Save the user input and any completed steps even when provider
            # calls fail or the user cancels the remaining execution.
            host._auto_save_session()
            capture_memories = getattr(host, "_maybe_capture_memories", None)
            if callable(capture_memories) and host.last_turn_status == "completed":
                capture_memories()
            trace_logger.log_event(
                "run_end",
                {"run_id": run_id, "final": response_text, "status": host.last_turn_status},
                step=0,
            )
            host.event_sink.emit(AgentEvent(EventType.RUN_FINISHED, {
                "run_id": run_id, "final": response_text, "status": host.last_turn_status,
            }))
        if host.console_progress:
            host._console("✅ Agent 已完成")

        host.logger.debug("response=%s", response_text)
        host.logger.info("history_size=%d, rounds=%d",
                        host.history_manager.get_message_count(),
                        host.history_manager.get_rounds_count())

        return response_text

