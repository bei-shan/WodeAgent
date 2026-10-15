"""Explicit per-Agent runtime service ownership with dependency validation.

Runtime services own long-lived resources (MCP connections, session storage,
team managers, sandboxes). Middleware/Features must not own those resources.
Graph registration is framework-owned; it is not a plugin capability API.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


class RuntimeService(Protocol):
    def start(self, agent: Any) -> None: ...
    def close(self, agent: Any) -> None: ...


@dataclass(frozen=True)
class ServiceDefinition:
    name: str
    factory: Callable[[], RuntimeService]
    requires: tuple[str, ...] = ()


class RuntimeServices:
    """Start dependencies first; close all entered services once, in reverse.

    A service instance is recorded *before* start(): a partially started
    service is still closed on init failure. No arbitrary plugin registration
    is exposed by CodeAgent in hosted mode.
    """

    def __init__(self, agent: Any):
        self.agent = agent
        self._definitions: dict[str, ServiceDefinition] = {}
        self._instances: dict[str, RuntimeService] = {}
        self._entered: list[str] = []
        self._starting: set[str] = set()
        self._closed = False

    def register(
        self, name: str, factory: Callable[[], RuntimeService],
        *, requires: tuple[str, ...] = (),
    ) -> None:
        if self._closed or self._entered:
            raise RuntimeError("Register services before starting the runtime")
        if (not isinstance(name, str) or not name or name in self._definitions
                or not callable(factory)):
            raise ValueError("Invalid or duplicate runtime service registration")
        if not isinstance(requires, tuple) or any(
            not isinstance(dep, str) or not dep or dep == name for dep in requires
        ) or len(set(requires)) != len(requires):
            raise ValueError("Invalid runtime service dependencies")
        self._definitions[name] = ServiceDefinition(name, factory, requires)

    def ensure(self, name: str) -> RuntimeService:
        if self._closed:
            raise RuntimeError("Runtime services already closed")
        if name in self._instances:
            return self._instances[name]
        if name in self._starting:
            raise RuntimeError(f"Cyclic runtime service dependency: {name}")
        spec = self._definitions.get(name)
        if spec is None:
            raise LookupError(f"Unknown runtime service: {name}")
        self._starting.add(name)
        try:
            for dependency in spec.requires:
                self.ensure(dependency)
            service = spec.factory()
            if not callable(getattr(service, "start", None)) or not callable(
                getattr(service, "close", None)
            ):
                raise TypeError(f"Invalid runtime service: {name}")
            self._instances[name] = service
            self._entered.append(name)
            service.start(self.agent)
            return service
        except BaseException:
            # Do not leave a half-started graph capable of being reused.
            # Teardown includes the service whose start() itself raised.
            if self._entered:
                self.close()
            raise
        finally:
            self._starting.remove(name)

    def get(self, name: str) -> RuntimeService | None:
        return self._instances.get(name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name in reversed(self._entered):
            try:
                self._instances[name].close(self.agent)
            except Exception:
                log.exception("Runtime service %s cleanup failed", name)

    @property
    def started(self) -> tuple[str, ...]:
        return tuple(self._entered)
