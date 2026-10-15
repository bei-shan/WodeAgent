"""Three-stage tool execution boundary.

prepare() executes policy/feature decisions on the Agent owner thread.
dispatch() may run on worker threads, but has no access to feature state.
publish() executes feature post-hooks on the Agent owner thread.
Agent history/event publication remains exclusively in CodeAgent.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class PreparedToolCall:
    name: str
    parameters: dict[str, Any]
    interceptors: tuple[ToolFeature, ...]
    started: float
    blocked_output: str | None = None


class ToolRuntime:
    """Feature decision, dispatch and publication, with explicit boundaries."""

    def __init__(
        self, dispatcher: ToolDispatcher, logger: logging.Logger, *,
        step_snapshot: Any = None,
    ):
        self.dispatcher = dispatcher
        self.logger = logger
        self.step_snapshot = step_snapshot

    def prepare(
        self, tool_name: str, tool_input: Any, *,
        agent: Any, features: Iterable[ToolFeature],
        emit_messages: Callable[[list[str]], None],
    ) -> PreparedToolCall:
        """Owner-thread phase: run all pre-hooks in order; deny before dispatch."""
        parameters = dict(tool_input) if isinstance(tool_input, dict) else {"input": tool_input}
        interceptors = tuple(features)
        started = time.monotonic()
        # Mandatory core policies execute BEFORE potentially mutating plugin
        # hooks; no plugin ordering or monkeypatched Feature list can bypass
        # plan/delegate/budget limits.
        policy = getattr(agent, "_policy_middleware", None)
        if policy is not None:
            pre_result = policy.before_tool(tool_name, parameters)
            if pre_result and pre_result.get("blocked"):
                payload = error_envelope(
                    str(pre_result.get("error_code", "PERMISSION_DENIED")),
                    str(pre_result.get("reason", "Blocked by core policy.")),
                    parameters,
                )
                return PreparedToolCall(
                    tool_name, parameters, interceptors, started,
                    json.dumps(payload, ensure_ascii=False),
                )
        for feature in interceptors:
            pre_result = feature.pre_tool_use(agent, tool_name, parameters)
            if pre_result and pre_result.get("blocked"):
                self.logger.info(
                    "Tool %s blocked by %s", tool_name, getattr(feature, "name", "policy"),
                )
                payload = error_envelope(
                    str(pre_result.get("error_code", "HOOK_BLOCKED")),
                    str(pre_result.get("reason", "Blocked")),
                    parameters,
                )
                return PreparedToolCall(
                    tool_name, parameters, interceptors, started,
                    json.dumps(payload, ensure_ascii=False),
                )
            if pre_result and pre_result.get("system_messages"):
                emit_messages(pre_result["system_messages"])
            if pre_result and pre_result.get("updated_input"):
                parameters = {**parameters, **pre_result["updated_input"]}
        return PreparedToolCall(tool_name, parameters, interceptors, started)

    def dispatch(self, call: PreparedToolCall) -> str:
        """Worker-safe phase: invoke dispatcher only, without hooks or UI."""
        from core.runtime.telemetry import get_telemetry
        with get_telemetry().span(
            "agent.tool", kind="tool", attributes={"ridge.tool.name": call.name},
        ) as observation:
            if call.blocked_output is not None:
                output = call.blocked_output
            else:
                if self.step_snapshot is not None:
                    # Snapshot is supported by the canonical ToolRegistry.
                    # Standalone integrations preserve their legacy port.
                    output = str(self.dispatcher.execute_tool(
                        call.name, call.parameters, snapshot=self.step_snapshot,
                    ))
                else:
                    output = str(self.dispatcher.execute_tool(call.name, call.parameters))
            try:
                status = json.loads(output).get("status")
                if status in ("success", "partial", "error"):
                    observation.attribute("ridge.tool.status", status)
            except (ValueError, TypeError, AttributeError):
                pass
            return output

    def publish(
        self, call: PreparedToolCall, output: str, *,
        agent: Any, emit_messages: Callable[[list[str]], None],
        record_timing: Callable[[str, int], None],
    ) -> str:
        """Owner-thread phase: record timing and publish post-hook messages."""
        if call.blocked_output is not None:
            return output
        record_timing(call.name, int((time.monotonic() - call.started) * 1000))
        for feature in call.interceptors:
            messages = feature.post_tool_use(agent, call.name, call.parameters, output)
            if messages:
                emit_messages(messages)
        return output

    def execute(
        self, tool_name: str, tool_input: Any, *,
        agent: Any, features: Iterable[ToolFeature],
        emit_messages: Callable[[list[str]], None],
        record_timing: Callable[[str, int], None],
    ) -> str:
        """Compatibility facade for standalone serial tool callers."""
        call = self.prepare(
            tool_name, tool_input, agent=agent, features=features,
            emit_messages=emit_messages,
        )
        output = self.dispatch(call)
        return self.publish(
            call, output, agent=agent, emit_messages=emit_messages,
            record_timing=record_timing,
        )
