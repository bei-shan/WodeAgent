"""Deterministic pruning of old tool outputs before summarization."""
from __future__ import annotations

from core.config import Config
from core.context_engine.history_manager import HistoryManager


def _manager() -> HistoryManager:
    return HistoryManager(config=Config())


def _round(manager: HistoryManager, index: int, size: int = 4000) -> None:
    manager.append_user(f"question {index}")
    manager.append_assistant(f"answer {index}")
    manager.append_tool("Bash", f"output-{index}-" + "x" * size)


def _tool_messages(manager: HistoryManager):
    # to_messages() folds tool results into user-role messages, so inspect the
    # durable Message objects that pruning actually edits.
    return [message for message in manager._messages if message.role == "tool"]


def test_old_tool_outputs_are_stubbed_and_recent_rounds_kept():
    manager = _manager()
    for index in range(6):
        _round(manager, index)

    pruned = manager.prune_tool_outputs(keep_rounds=2, min_chars=100)
    assert pruned == 4

    tools = _tool_messages(manager)
    assert len(tools) == 6
    assert tools[-1].content.startswith("output-5-")
    assert tools[-2].content.startswith("output-4-")
    for message in tools[:4]:
        assert "re-run the tool" in message.content
        assert message.metadata["pruned"] is True
        assert message.metadata["original_chars"] > 4000
    # Tool calls are untouched, so pairing stays valid.
    assert len([m for m in manager._messages if m.role == "assistant"]) == 6


def test_prune_is_idempotent_and_ignores_small_outputs():
    manager = _manager()
    for index in range(4):
        _round(manager, index, size=50)
    assert manager.prune_tool_outputs(keep_rounds=1, min_chars=600) == 0

    # Rounds 4 and 5 are large; with keep_rounds=1 only round 4 falls behind
    # the cutoff.
    for index in range(4, 6):
        _round(manager, index, size=4000)
    assert manager.prune_tool_outputs(keep_rounds=1, min_chars=600) == 1
    assert manager.prune_tool_outputs(keep_rounds=1, min_chars=600) == 0


def test_short_sessions_are_left_alone():
    manager = _manager()
    for index in range(3):
        _round(manager, index)
    assert manager.prune_tool_outputs(keep_rounds=5, min_chars=10) == 0


def test_prune_config_defaults():
    config = Config()
    assert config.tool_prune_enabled is True
    assert 0 < config.tool_prune_soft_ratio < 1
    assert config.tool_prune_keep_rounds >= 1
    assert config.tool_prune_min_chars > 0
