"""Shared helpers for parsing LLM (OpenAI-compatible) responses.

Extracted from ``CodeAgent`` and ``TurnExecutor`` to eliminate code
duplication.  All functions are pure — no side effects, no class state.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Tuple


def _get_attr(obj: Any, key: str) -> Any:
    """Safely read *key* from a dict or object, returning None on miss."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def extract_content(raw_response: Any) -> Optional[str]:
    """Extract the text ``content`` from an OpenAI ChatCompletion response."""
    try:
        if hasattr(raw_response, "choices"):
            content = raw_response.choices[0].message.content
            if isinstance(content, list):
                return "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return content
        if isinstance(raw_response, dict) and raw_response.get("choices"):
            content = raw_response["choices"][0]["message"].get("content")
            if isinstance(content, list):
                return "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return content
    except Exception:
        return str(raw_response)
    return None


def extract_reasoning_content(raw_response: Any) -> Optional[str]:
    """Extract ``reasoning_content`` (DeepSeek/Qwen/Kimi reasoning models).

    Checks ``message.reasoning_content``, ``message.reasoning``, and
    ``message.model_extra`` / ``message.additional_kwargs``.
    """
    try:
        choices = _get_attr(raw_response, "choices")
        if not choices:
            return None
        message = _get_attr(choices[0], "message")
        if not message:
            return None

        reasoning = _get_attr(message, "reasoning_content") or _get_attr(
            message, "reasoning"
        )
        if reasoning:
            return reasoning

        model_extra = _get_attr(message, "model_extra") or _get_attr(
            message, "additional_kwargs"
        )
        if isinstance(model_extra, dict):
            return model_extra.get("reasoning_content") or model_extra.get(
                "reasoning"
            )
    except Exception:
        return None
    return None


def extract_usage(raw_response: Any) -> Optional[dict]:
    """Extract token usage from an OpenAI ChatCompletion response."""
    try:
        if hasattr(raw_response, "usage"):
            usage = raw_response.usage
            if not usage:
                return None
            return {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        if isinstance(raw_response, dict) and raw_response.get("usage"):
            usage = raw_response["usage"]
            return {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
    except Exception:
        return None
    return None


def extract_tool_calls(raw_response: Any) -> list[dict[str, Any]]:
    """Extract tool_calls from an OpenAI ChatCompletion response.

    Returns a list of ``{id, name, arguments}`` dicts.  Also handles the
    legacy ``function_call`` field for older API versions.
    """
    try:
        choices = _get_attr(raw_response, "choices")
        if not choices:
            return []
        message = _get_attr(choices[0], "message")
        if not message:
            return []
        tool_calls = _get_attr(message, "tool_calls") or []
        calls: list[dict[str, Any]] = []
        if tool_calls:
            for call in tool_calls:
                fn = _get_attr(call, "function") or {}
                name = (
                    _get_attr(fn, "name")
                    or _get_attr(call, "name")
                    or "unknown_tool"
                )
                arguments = (
                    _get_attr(fn, "arguments")
                    or _get_attr(call, "arguments")
                    or {}
                )
                call_id = _get_attr(call, "id")
                calls.append(
                    {"id": call_id, "name": name, "arguments": arguments}
                )
            return calls

        # Legacy function_call fallback (older OpenAI API)
        function_call = _get_attr(message, "function_call")
        if function_call:
            name = _get_attr(function_call, "name") or "unknown_tool"
            arguments = _get_attr(function_call, "arguments") or {}
            return [{"id": None, "name": name, "arguments": arguments}]
    except Exception:
        return []
    return []


def extract_response_meta(raw_response: Any) -> dict:
    """Extract response metadata for debugging empty responses."""
    meta: dict = {}
    try:
        choices = _get_attr(raw_response, "choices") or []
        if not choices:
            return meta
        choice = choices[0]
        meta["finish_reason"] = _get_attr(choice, "finish_reason")
        message = _get_attr(choice, "message")
        if not message:
            return meta
        meta["role"] = _get_attr(message, "role")

        content = _get_attr(message, "content")
        reasoning_content = (
            _get_attr(message, "reasoning_content")
            or _get_attr(message, "reasoning")
        )
        refusal = _get_attr(message, "refusal")
        tool_calls = _get_attr(message, "tool_calls")
        function_call = _get_attr(message, "function_call")

        meta["content_len"] = len(str(content)) if content is not None else 0
        meta["reasoning_len"] = (
            len(str(reasoning_content)) if reasoning_content is not None else 0
        )
        meta["refusal_present"] = refusal is not None
        meta["tool_calls_count"] = (
            len(tool_calls)
            if isinstance(tool_calls, list)
            else (1 if tool_calls else 0)
        )
        meta["function_call_present"] = function_call is not None
    except Exception:
        return meta
    return meta


def ensure_json_input(raw: Any) -> Tuple[Any, Optional[str]]:
    """Parse a tool-call ``arguments`` value into a dict/list.

    Returns ``(parsed, error_message)``.  *error_message* is None on success.
    """
    if raw is None:
        return {}, None
    if isinstance(raw, (dict, list)):
        return raw, None
    s = str(raw).strip()
    if not s:
        return {}, None
    try:
        return json.loads(s), None
    except Exception as e:
        return None, str(e)
