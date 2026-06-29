"""P1-D TUI streaming.py activation tests.

Run: python -m pytest tests/test_tui_streaming.py -v"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from tui.streaming import StreamingResponse


@pytest.fixture
def console() -> Console:
    """Fresh console per test — Rich forbids concurrent Live on one Console."""
    return Console(file=MagicMock(), force_terminal=True, color_system="truecolor")


def test_append_collects_content(console):
    stream = StreamingResponse(console)
    stream.start("Agent")
    stream.append("Hello ")
    stream.append("World")
    content, reasoning, elapsed, tokens = stream.finish()
    assert content == "Hello World"
    assert reasoning == ""
    assert tokens == 2


def test_append_reasoning_collects_separately(console):
    stream = StreamingResponse(console)
    stream.start("Agent")
    stream.append_reasoning("Let me think...")
    stream.append_reasoning(" step by step.")
    stream.append("Final answer.")
    content, reasoning, elapsed, tokens = stream.finish()
    assert "Let me think" in reasoning
    assert "step by step" in reasoning
    assert content == "Final answer."
    assert tokens == 3


def test_finish_returns_token_count(console):
    stream = StreamingResponse(console)
    stream.start("Agent")
    stream.append("a")
    stream.append_reasoning("b")
    _c, _r, _e, tokens = stream.finish()
    assert tokens == 2


def test_elapsed_property(console):
    stream = StreamingResponse(console)
    assert stream.elapsed == 0.0
    stream.start("Agent")
    time.sleep(0.05)
    assert stream.elapsed >= 0.04


def test_update_title(console):
    stream = StreamingResponse(console)
    stream.start("Agent")
    stream.update_title("Step 3 — Agent")
    stream.finish()
    assert stream._title == "Step 3 — Agent"


def test_finish_without_start_no_crash(console):
    """finish() without a prior start() should not raise."""
    stream = StreamingResponse(console)
    content, reasoning, elapsed, tokens = stream.finish()
    assert content == ""
    assert reasoning == ""
    assert tokens == 0


def test_legacy_append_only_still_works(console):
    stream = StreamingResponse(console)
    stream.start("Agent")
    stream.append("chunk1")
    stream.append("chunk2")
    content, reasoning, elapsed, tokens = stream.finish()
    assert content == "chunk1chunk2"
    assert reasoning == ""
    assert tokens == 2
