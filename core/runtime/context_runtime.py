"""Context preparation/compaction port for one ReAct step.

HistoryManager owns durable messages and compaction; ContextBuilder owns system
blocks. ContextRuntime coordinates the two without importing CodeAgent.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol


class ContextRuntimePort(Protocol):
    history_manager: Any
    context_builder: Any
    config: Any
    logger: logging.Logger
    console_verbose: bool

    def _console(self, text: str) -> None: ...
    def _print_context_preview(self, messages: list[dict]) -> None: ...
    def _build_messages(self, history_messages: list[dict]) -> list[dict]: ...


class ContextRuntime:
    def __init__(self, host: ContextRuntimePort):
        self.host = host

    def maybe_compress_history(self, pending_input: str, trace_logger, step: int) -> None:
        """检查并在需要时压缩历史。"""
        host = self.host
        if not host.history_manager.should_compress(pending_input):
            return

        estimated_tokens = host.history_manager.estimate_context_tokens(pending_input)
        threshold = int(host.config.context_window * host.config.compression_threshold)
        trace_logger.log_event("history_compression_triggered", {
            "estimated_tokens": estimated_tokens,
            "threshold": threshold,
            "total_usage_tokens": host.history_manager.get_total_usage_tokens(),
            "message_count": host.history_manager.get_message_count(),
        }, step=step)

        if host.console_verbose:
            host._console("\n📦 触发历史压缩...")
        elif host.logger.isEnabledFor(logging.DEBUG):
            host.logger.debug("触发历史压缩")

        rounds_before = host.history_manager.get_rounds_count()
        messages_before = host.history_manager.get_message_count()

        compress_info = host.history_manager.compact(
            on_event=lambda ev, payload: trace_logger.log_event(ev, payload, step=step),
            return_info=True,
        )
        compressed = bool(compress_info.get("compressed"))

        if compressed:
            rounds_after = host.history_manager.get_rounds_count()
            messages_after = host.history_manager.get_message_count()

            trace_logger.log_event("history_compression_completed", {
                "rounds_before": rounds_before,
                "rounds_after": rounds_after,
                "messages_compressed": messages_before - messages_after,
                "summary_generated": compress_info.get("summary_generated", False),
                "details": compress_info,
            }, step=step)

            compressed_history = (
                host.history_manager.to_messages_branch()
                if hasattr(host.history_manager, "to_messages_branch")
                else host.history_manager.to_messages()
            )
            final_context = host.context_builder.build_messages(compressed_history)
            trace_logger.log_event(
                "history_compression_final_context",
                {"message_count": len(final_context), "messages": final_context},
                step=step,
            )

            if host.console_verbose:
                host._console(f"✅ 压缩完成，当前轮次数: {rounds_after}")
                host._print_context_preview(final_context)
            elif host.logger.isEnabledFor(logging.DEBUG):
                host.logger.debug("压缩完成，当前轮次数: %d", rounds_after)
                host._print_context_preview(final_context)

    def build_step_messages(self, trace_logger, step: int):
        """构建当前 step 的 messages 列表，返回 (messages, base_messages)。"""
        host = self.host
        # Branch-sensitive prompts must not include abandoned sibling paths.
        history_messages = (
            host.history_manager.to_messages_branch()
            if hasattr(host.history_manager, "to_messages_branch")
            else host.history_manager.to_messages()
        )
        messages = host._build_messages(history_messages)
        base_messages = messages
        trace_logger.log_event(
            "context_build",
            {"message_count": len(messages), "history_count": len(history_messages)},
            step=step,
        )
        return messages, base_messages

