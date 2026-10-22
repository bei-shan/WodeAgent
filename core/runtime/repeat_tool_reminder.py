"""Advisory loop hygiene: notice when the model repeats one exact call.

Ported behaviour from DeepSeek Harness' ``repeat-tool-reminder``:

* advisory only - it never blocks or rewrites the call;
* exact match on ``(tool name, canonical arguments)``;
* excluded tools are transparent to the chain, so a bookkeeping call cannot
  mask a loop;
* thresholds fire exactly once each (default 3, 5 and 8 consecutive calls);
* chains are per agent and live only in memory.
"""
from __future__ import annotations

import json
import weakref
from typing import Any, Iterable

DEFAULT_THRESHOLDS = (3, 5, 8)
DEFAULT_EXCLUDE = ("TodoWrite", "todo_write", "AskUser", "ask_user")
DEFAULT_PREVIEW_CHARS = 500


class RepeatToolReminder:
    """Tool interceptor implementing the three-phase runtime protocol."""

    name = "repeat_tool_reminder"

    def __init__(
        self,
        thresholds: Iterable[int] = DEFAULT_THRESHOLDS,
        exclude: Iterable[str] = DEFAULT_EXCLUDE,
        arguments_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ):
        values = tuple(sorted({int(value) for value in thresholds}))
        if not values:
            raise ValueError("thresholds must not be empty")
        if values[0] < 2:
            raise ValueError("repeat thresholds must be >= 2")
        self._thresholds = values
        self._exclude = frozenset(exclude)
        self._preview_chars = max(0, int(arguments_preview_chars))
        self._chains: "weakref.WeakKeyDictionary[Any, tuple[str, str, int]]" = (
            weakref.WeakKeyDictionary()
        )

    # ------------------------------------------------------------------
    # Interceptor protocol
    # ------------------------------------------------------------------
    def pre_tool_use(self, agent: Any, tool_name: str, parameters: dict[str, Any]):
        return None

    def post_tool_use(
        self, agent: Any, tool_name: str, parameters: dict[str, Any], output: str,
    ) -> list[str] | None:
        if agent is None or tool_name in self._exclude:
            return None
        key = self._canonical(parameters)
        previous = self._chains.get(agent)
        if previous and previous[0] == tool_name and previous[1] == key:
            count = previous[2] + 1
        else:
            count = 1
        self._chains[agent] = (tool_name, key, count)
        if count not in self._thresholds:
            return None
        return [self._message(tool_name, count, key)]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def reset(self, agent: Any) -> None:
        """Forget one agent's chain (used when a new user message arrives)."""
        self._chains.pop(agent, None)

    def chain_length(self, agent: Any) -> int:
        entry = self._chains.get(agent)
        return entry[2] if entry else 0

    @staticmethod
    def _canonical(parameters: Any) -> str:
        try:
            return json.dumps(
                parameters, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return str(parameters)

    def _message(self, tool_name: str, count: int, key: str) -> str:
        if count == self._thresholds[0]:
            return (
                "You are repeating the exact same tool call with identical arguments. "
                "Analyze the previous result before calling again: if the task is not "
                "complete, try a different approach or different arguments instead of "
                "repeating the call."
            )
        preview = key
        if len(preview) > self._preview_chars:
            omitted = len(preview) - self._preview_chars
            preview = f"{preview[: self._preview_chars]}… (+{omitted} more chars)"
        return (
            "Repeated tool call detected:\n"
            f"- tool: {tool_name}\n"
            f"- consecutive_calls: {count}\n"
            f"- arguments: {preview}\n"
            "The repeated calls are not making progress. Do not call this tool with "
            "these exact arguments again. Inspect the latest result and choose a "
            "different action, different arguments, or finish the task if enough "
            "evidence has been gathered."
        )
