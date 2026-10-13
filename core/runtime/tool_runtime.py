"""Feature-aware ToolRuntime and typed ports, with no CodeAgent/UI imports.

CodeAgent owns the turn loop and event lifecycle. ToolRuntime owns exactly
one dispatch through before/after feature interceptors, tool execution, and
elapsed-time accounting. This is an internal extraction, not a second engine.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Iterable, Protocol

from core.runtime.tool_contracts import error_envelope


class ToolDispatcher(Protocol):
    def execute_tool(self, name: str, parameters: dict[str, Any]) -> Any: ...


class ToolFeature(Protocol):
    name: str

    def pre_tool_use(
        self, agent: Any, tool_name: str, parameters: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    def post_tool_use(
        self, agent: Any, tool_name: str, parameters: dict[str, Any],
        output: str,
    ) -> list[str] | None: ...


class ToolRuntime:
    """Runs a single tool without owning the Agent or its mutable turn state."""

    def __init__(self, dispatcher: ToolDispatcher, logger: logging.Logger):
        self.dispatcher = dispatcher
        self.logger = logger

    def execute(
        self,
        tool_name: str,
        tool_input: Any,
        *,
        agent: Any,
        features: Iterable[ToolFeature],
        emit_messages: Callable[[list[str]], None],
        record_timing: Callable[[str, int], None],
    ) -> str:
        from core.runtime.telemetry import get_telemetry
        with get_telemetry().span(
            "agent.tool", kind="tool",
            attributes={"ridge.tool.name": tool_name},
        ) as observation:
            result = self._execute_untraced(
                tool_name, tool_input, agent=agent, features=features,
                emit_messages=emit_messages, record_timing=record_timing,
            )
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    status = parsed.get("status")
                    if status in ("success", "partial", "error"):
                        observation.attribute("ridge.tool.status", status)
            except (ValueError, TypeError):
                pass
            return result

    def _execute_untraced(
        self, tool_name: str, tool_input: Any, *,
        agent: Any, features: Iterable[ToolFeature],
        emit_messages: Callable[[list[str]], None],
        record_timing: Callable[[str, int], None],
    ) -> str:
        parameters = tool_input if isinstance(tool_input, dict) else {"input": tool_input}
        started = time.monotonic()
        # Features may be a generator; all interceptors must see the same order
        # before and after dispatch even if the source iterable is consumed.
        interceptors = tuple(features)

        for feature in interceptors:
            pre_result = feature.pre_tool_use(agent, tool_name, parameters)
            if pre_result and pre_result.get("blocked"):
                self.logger.info(
                    "Tool %s blocked by %s: %s",
                    tool_name, feature.name, pre_result.get("reason", "unknown"),
                )
                payload = error_envelope(
                    str(pre_result.get("error_code", "HOOK_BLOCKED")),
                    str(pre_result.get("reason", "Blocked")),
                    parameters,
                )
                return json.dumps(payload, ensure_ascii=False)
            if pre_result and pre_result.get("system_messages"):
                emit_messages(pre_result["system_messages"])
            if pre_result and pre_result.get("updated_input"):
                parameters = {**parameters, **pre_result["updated_input"]}

        output = self.dispatcher.execute_tool(tool_name, parameters)
        record_timing(tool_name, int((time.monotonic() - started) * 1000))

        for feature in interceptors:
            messages = feature.post_tool_use(agent, tool_name, parameters, str(output))
            if messages:
                emit_messages(messages)
        return str(output)
