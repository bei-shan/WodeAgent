"""Context-local Agent configuration; protects concurrent session turns."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from core.config import Config

_CURRENT_CONFIG: ContextVar["Config | None"] = ContextVar(
    "wodeagent_agent_config", default=None,
)


def active_config() -> "Config | None":
    return _CURRENT_CONFIG.get()


@contextmanager
def scoped_config(config: "Config") -> "Iterator[None]":
    token = _CURRENT_CONFIG.set(config)
    try:
        yield
    finally:
        _CURRENT_CONFIG.reset(token)


def bind_current_context(callback):
    """Return a callback bound to a *snapshot* of the caller's context.

    Launch it on another thread at most once; every launch needs its own
    copy_context() because Context cannot be entered concurrently.
    """
    from contextvars import copy_context
    context = copy_context()

    def invoke(*args, **kwargs):
        return context.run(callback, *args, **kwargs)

    return invoke
