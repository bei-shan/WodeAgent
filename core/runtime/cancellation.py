"""Per-invocation cooperative cancellation without modifying LLM tool inputs."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
from typing import Iterator

_cancel_event: ContextVar[Event | None] = ContextVar("agent_tool_cancel_event", default=None)


def current_cancel_event() -> Event | None:
    return _cancel_event.get()


@contextmanager
def scoped_tool_cancellation(event: Event | None) -> Iterator[None]:
    token = _cancel_event.set(event)
    try:
        yield
    finally:
        _cancel_event.reset(token)
