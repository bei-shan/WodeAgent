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
