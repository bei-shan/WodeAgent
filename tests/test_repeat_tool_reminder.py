"""Repeat-tool reminder: advisory, exact-match, per-agent, threshold-once."""
from __future__ import annotations

import pytest

from core.runtime.repeat_tool_reminder import RepeatToolReminder


class _Agent:
    """Hashable stand-in for a live Agent handle."""


def test_reminds_only_at_each_threshold():
    reminder = RepeatToolReminder(thresholds=(3, 5), arguments_preview_chars=50)
    agent = _Agent()
    calls = {"name": "Grep", "path": "src"}

    fired = [i for i in range(1, 8) if reminder.post_tool_use(agent, "Grep", calls, "out")]
    assert fired == [3, 5]


def test_first_threshold_is_short_and_later_is_detailed():
    reminder = RepeatToolReminder(thresholds=(3, 5))
    agent = _Agent()
    params = {"name": "Grep", "pattern": "x"}
    messages = []
    for _ in range(5):
        messages.append(reminder.post_tool_use(agent, "Grep", params, ""))
    assert "repeating the exact same tool call" in messages[2][0]
    assert "Repeated tool call detected" in messages[4][0]
    assert "consecutive_calls: 5" in messages[4][0]


def test_different_arguments_reset_the_chain():
    reminder = RepeatToolReminder(thresholds=(2,))
    agent = _Agent()
    assert reminder.post_tool_use(agent, "Grep", {"pattern": "a"}, "") is None
    assert reminder.post_tool_use(agent, "Grep", {"pattern": "b"}, "") is None
    assert reminder.post_tool_use(agent, "Grep", {"pattern": "b"}, "") is not None


def test_argument_order_does_not_matter():
    reminder = RepeatToolReminder(thresholds=(2,))
    agent = _Agent()
    reminder.post_tool_use(agent, "Bash", {"a": 1, "b": 2}, "")
    assert reminder.post_tool_use(agent, "Bash", {"b": 2, "a": 1}, "") is not None


def test_excluded_tools_are_transparent_to_the_chain():
    reminder = RepeatToolReminder(thresholds=(2,), exclude=("TodoWrite",))
    agent = _Agent()
    reminder.post_tool_use(agent, "Grep", {"pattern": "a"}, "")
    assert reminder.post_tool_use(agent, "TodoWrite", {"items": []}, "") is None
    assert reminder.post_tool_use(agent, "Grep", {"pattern": "a"}, "") is not None


def test_chains_are_isolated_per_agent():
    reminder = RepeatToolReminder(thresholds=(2,))
    first, second = _Agent(), _Agent()
    reminder.post_tool_use(first, "Grep", {"pattern": "a"}, "")
    assert reminder.post_tool_use(second, "Grep", {"pattern": "a"}, "") is None
    assert reminder.post_tool_use(first, "Grep", {"pattern": "a"}, "") is not None


def test_reset_clears_one_agent():
    reminder = RepeatToolReminder(thresholds=(2,))
    agent = _Agent()
    reminder.post_tool_use(agent, "Grep", {"pattern": "a"}, "")
    reminder.reset(agent)
    assert reminder.chain_length(agent) == 0
    assert reminder.post_tool_use(agent, "Grep", {"pattern": "a"}, "") is None


def test_rejects_invalid_thresholds():
    with pytest.raises(ValueError):
        RepeatToolReminder(thresholds=())
    with pytest.raises(ValueError):
        RepeatToolReminder(thresholds=(1,))


def test_preview_is_capped():
    reminder = RepeatToolReminder(thresholds=(2, 4), arguments_preview_chars=10)
    agent = _Agent()
    params = {"payload": "x" * 500}
    reminder.post_tool_use(agent, "Write", params, "")  # 1
    first = reminder.post_tool_use(agent, "Write", params, "")[0]  # 2 -> short
    reminder.post_tool_use(agent, "Write", params, "")  # 3
    detailed = reminder.post_tool_use(agent, "Write", params, "")[0]  # 4 -> detailed
    assert "more chars)" in detailed
    assert "more chars)" not in first
