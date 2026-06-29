"""Single-turn execution kernel shared by oneshot and persistent workers."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from core.context_engine.observation_truncator import truncate_observation
from core.response_parser import (
    extract_content,
    extract_tool_calls,
    ensure_json_input,
    extract_reasoning_content,
)
from tools.registry import ToolRegistry


class TurnExecutor:
    def __init__(
        self,
        llm: Any,
        tool_registry: ToolRegistry,
        project_root: Path,
        denied_tools: Optional[set[str]] = None,
    ):
        self.llm = llm
        self.tool_registry = tool_registry
        self.project_root = Path(project_root)
        self.denied_tools = set(denied_tools or set())
        self._tools_schema = self._get_tools_schema()

    def execute_turn(
        self,
        messages: list[dict[str, Any]],
        tool_usage: Dict[str, int],
        on_delta: Optional[Callable[[str, str], None]] = None,
        event_bridge: Any = None,
        subagent_step: Optional[int] = None,
    ) -> Dict[str, Any]:
        raw_response = self._invoke_llm(messages, on_delta=on_delta)
        response_text = extract_content(raw_response) or ""
        reasoning_content = extract_reasoning_content(raw_response)
        tool_calls = extract_tool_calls(raw_response)
        output_messages = list(messages)

        assistant_msg: Dict[str, Any] = {"role": "assistant", "content": response_text}
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        if tool_calls:
            assistant_msg["tool_calls"] = []
            for call in tool_calls:
                call_id = call.get("id") or f"call_{uuid.uuid4().hex}"
                call["id"] = call_id
                arguments = call.get("arguments") or {}
                args_str = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
                assistant_msg["tool_calls"].append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call.get("name"), "arguments": args_str},
                })
        output_messages.append(assistant_msg)

        if not tool_calls:
            return {
                "done": True,
                "final_result": self._extract_final_answer(response_text),
                "messages": output_messages,
                "assistant_text": response_text,
                "tool_calls": [],
            }

        for call in tool_calls:
            tool_name = call.get("name") or "unknown_tool"
            tool_call_id = call.get("id") or f"call_{uuid.uuid4().hex}"
            tool_input, parse_err = ensure_json_input(call.get("arguments"))
            if parse_err:
                observation = json.dumps(
                    {
                        "status": "error",
                        "error": {"code": "INVALID_PARAM", "message": f"Tool arguments parse error: {parse_err}"},
                        "data": {},
                    },
                    ensure_ascii=False,
                )
            else:
                if event_bridge is not None:
                    event_bridge.emit_tool(
                        phase="started",
                        name=tool_name,
                        args=tool_input if isinstance(tool_input, dict) else {},
                        subagent_step=subagent_step,
                    )
                observation = self._execute_tool(tool_name, tool_input, tool_usage)
                if event_bridge is not None:
                    event_bridge.emit_tool(
                        phase="completed",
                        name=tool_name,
                        result_preview=str(observation)[:200],
                        subagent_step=subagent_step,
                    )

            output_messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": observation,
            })

        return {
            "done": False,
            "final_result": None,
            "messages": output_messages,
            "assistant_text": response_text,
            "tool_calls": [
                {"name": c.get("name"), "arguments": c.get("arguments")}
                for c in tool_calls
            ],
        }

    def _invoke_llm(
        self,
        messages: list[dict[str, Any]],
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> Any:
        """Invoke the LLM, using streaming when a delta callback is provided.

        The non-streaming path is unchanged for existing callers. Streaming
        callers get token-level deltas via ``on_delta(kind, text)`` and the
        same merged raw response shape as ``invoke_raw`` via stream_raw(). If
        a provider lacks streaming or raises in the streaming path, fall back
        to invoke_raw so subagent execution remains correct (visibility degrades).
        """
        if on_delta is not None and hasattr(self.llm, "stream_raw"):
            try:
                return self.llm.stream_raw(
                    messages,
                    tools=self._tools_schema,
                    tool_choice="auto",
                    on_delta=on_delta,
                )
            except Exception:
                # Streaming is observability, not correctness. Degrade to the
                # known-good non-streaming path if a provider rejects streaming.
                pass
        return self.llm.invoke_raw(messages, tools=self._tools_schema, tool_choice="auto")

    def _get_tools_schema(self) -> list[dict[str, Any]]:
        tools = self.tool_registry.get_openai_tools()
        if not self.denied_tools:
            return tools
        return [
            item for item in tools
            if item.get("function", {}).get("name") not in self.denied_tools
        ]

    def _execute_tool(self, tool_name: str, tool_input: Dict[str, Any], tool_usage: Dict[str, int]) -> str:
        if tool_name in self.denied_tools:
            return f"Error: Tool '{tool_name}' is not allowed for subagents."

        tool = self.tool_registry.get_tool(tool_name)
        if tool is None:
            return f"Error: Tool '{tool_name}' not found."

        tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1
        try:
            result = tool.run(tool_input)
            return truncate_observation(tool_name, str(result), str(self.project_root))
        except Exception as exc:
            return f"Error executing tool: {exc}"

    @staticmethod
    def _extract_content(raw_response: Any) -> Optional[str]:
        try:
            if hasattr(raw_response, "choices"):
                content = raw_response.choices[0].message.content
                if isinstance(content, list):
                    return "".join(part.get("text", "") for part in content if isinstance(part, dict))
                return content
            if isinstance(raw_response, dict) and raw_response.get("choices"):
                content = raw_response["choices"][0]["message"].get("content")
                if isinstance(content, list):
                    return "".join(part.get("text", "") for part in content if isinstance(part, dict))
                return content
        except Exception:
            return str(raw_response)
        return None

    @staticmethod
    def _extract_tool_calls(raw_response: Any) -> list[dict[str, Any]]:
        def _get_attr(obj, key: str):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj.get(key)
            return getattr(obj, key, None)

        try:
            choices = _get_attr(raw_response, "choices")
            if not choices:
                return []
            choice = choices[0]
            message = _get_attr(choice, "message")
            if not message:
                return []
            tool_calls = _get_attr(message, "tool_calls") or []
            calls: list[dict[str, Any]] = []
            if tool_calls:
                for call in tool_calls:
                    fn = _get_attr(call, "function") or {}
                    name = _get_attr(fn, "name") or _get_attr(call, "name") or "unknown_tool"
                    arguments = _get_attr(fn, "arguments") or _get_attr(call, "arguments") or {}
                    call_id = _get_attr(call, "id")
                    calls.append({"id": call_id, "name": name, "arguments": arguments})
                return calls
        except Exception:
            return []
        return []

    @staticmethod
    def _extract_final_answer(response: str) -> str:
        return (response or "").strip()

    @staticmethod
    def _ensure_json_input(raw: Any) -> Tuple[Any, Optional[str]]:
        if raw is None:
            return {}, None
        if isinstance(raw, (dict, list)):
            return raw, None
        s = str(raw).strip()
        if not s:
            return {}, None
        try:
            return json.loads(s), None
        except Exception as exc:
            return None, str(exc)

