"""Explicit opt-in backend selection. Fail closed if a plugin is unavailable."""

from __future__ import annotations

import threading
from typing import Mapping

from .contracts import MemoryBackend, MemoryRecord, MemoryScope

KNOWN_PROVIDERS = ("off", "sqlite", "mem0", "openviking", "tencentdb")


class MemoryProviderUnavailable(RuntimeError):
    pass


class DisabledMemoryBackend:
    def add(self, scope: MemoryScope, content: str, *, metadata=None) -> MemoryRecord:
        raise MemoryProviderUnavailable("Long-term memory is disabled")

    def search(self, scope: MemoryScope, query: str, *, limit: int = 8) -> list[MemoryRecord]:
        return []

    def delete(self, scope: MemoryScope, record_id: str) -> bool:
        return False


class ScopedMemoryHandle:
    """A scope-bound view, not a raw provider or an authentication credential.

    The authenticated application layer must derive the scope from a verified
    principal before requesting this handle. Every operation rejects a
    mismatched scope, and resolves the provider dynamically so switching
    to 'off' revokes even cached handles for subsequent calls.
    """

    def __init__(self, registry: "MemoryProviderRegistry", scope: MemoryScope):
        self._registry = registry
        self.scope = scope

    def _resolve(self, scope: MemoryScope) -> MemoryBackend:
        if not isinstance(scope, MemoryScope) or scope != self.scope:
            raise PermissionError("Memory handle is bound to another scope")
        return self._registry._current_backend(self.scope)

    def add(self, scope: MemoryScope, content: str, *, metadata=None) -> MemoryRecord:
        return self._resolve(scope).add(self.scope, content, metadata=metadata)

    def search(self, scope: MemoryScope, query: str, *, limit: int = 8) -> list[MemoryRecord]:
        return self._resolve(scope).search(self.scope, query, limit=limit)

    def delete(self, scope: MemoryScope, record_id: str) -> bool:
        return self._resolve(scope).delete(self.scope, record_id)


class MemoryProviderRegistry:
    """Backend selection for an authenticated scope, process-local for now.

    In production, selections must be persisted in the authorized settings DB
    and loaded *after* authentication. This registry intentionally cannot grant
    access to a new MemoryScope or silently fall back to a different provider.
    """

    def __init__(self, providers: Mapping[str, MemoryBackend] | None = None):
        self._lock = threading.RLock()
        self._providers: dict[str, MemoryBackend] = {"off": DisabledMemoryBackend()}
        self._selection: dict[MemoryScope, str] = {}
        for name, backend in (providers or {}).items():
            self.register(name, backend)

    def register(self, name: str, backend: MemoryBackend) -> None:
        if name not in KNOWN_PROVIDERS or name == "off":
            raise ValueError(f"Unknown or reserved memory provider: {name}")
        if not all(callable(getattr(backend, m, None)) for m in ("add", "search", "delete")):
            raise TypeError("Backend must implement add/search/delete")
        with self._lock:
            self._providers[name] = backend

    def status(self) -> dict[str, bool]:
        with self._lock:
            return {name: name in self._providers for name in KNOWN_PROVIDERS}

    def select(self, scope: MemoryScope, provider: str) -> None:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Authenticated MemoryScope required")
        with self._lock:
            if provider not in self._providers:
                raise MemoryProviderUnavailable(
                    f"Memory provider '{provider}' is not installed or configured"
                )
            self._selection[scope] = provider

    def selected(self, scope: MemoryScope) -> str:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Authenticated MemoryScope required")
        with self._lock:
            return self._selection.get(scope, "off")

    def _current_backend(self, scope: MemoryScope) -> MemoryBackend:
        with self._lock:
            provider = self._selection.get(scope, "off")
            # A provider registration may be intentionally omitted in a new
            # deployment. Never silently switch to another memory backend.
            if provider not in self._providers:
                raise MemoryProviderUnavailable("Selected memory provider is not configured")
            return self._providers[provider]

    def for_scope(self, scope: MemoryScope) -> ScopedMemoryHandle:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Authenticated MemoryScope required")
        return ScopedMemoryHandle(self, scope)
