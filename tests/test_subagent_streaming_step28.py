"""Subagent events remain exposed through the server wire protocol.

The removed Python terminal's Rich sink/buffering tests moved out with
the legacy UI. Terminal projection is tested by tui-v2/test/reducer.test.ts.
"""
from __future__ import annotations

from core.events import EventType


def test_stream_events_includes_subagent_types():
    """The FastAPI WebSocket whitelist must include all subagent events."""
    from desktop.service.app import _STREAM_EVENTS

    assert EventType.SUBAGENT_STARTED in _STREAM_EVENTS
    assert EventType.SUBAGENT_DELTA in _STREAM_EVENTS
    assert EventType.SUBAGENT_TOOL_USE in _STREAM_EVENTS
    assert EventType.SUBAGENT_FINISHED in _STREAM_EVENTS
