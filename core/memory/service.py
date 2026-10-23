"""Local memory service: settings + provider selection + scope-bound access.

One short-lived instance per request or tool call, so a settings change made in
the Web UI applies to the next call without restarting anything.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import MemoryRecord, MemoryScope
from .registry import MemoryProviderRegistry, MemoryProviderUnavailable
from .settings import (
    AVAILABLE_PROVIDERS,
    MemorySettings,
    MemorySettingsError,
    load_settings,
    local_scope,
    save_settings,
    store_path,
)
from .sqlite_backend import SQLiteMemoryBackend


class LocalMemoryService:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root)
        self.settings: MemorySettings = load_settings(self.project_root)
        self.scope: MemoryScope = local_scope(self.project_root)
        self.registry = MemoryProviderRegistry()
        self._provider_error: str = ""
        try:
            self.registry.register(
                "sqlite", SQLiteMemoryBackend(store_path(self.project_root)),
            )
        except Exception as exc:  # a broken store must not crash the app
            self._provider_error = str(exc)
        if self.settings.enabled:
            try:
                self.registry.select(self.scope, self.settings.provider)
            except MemoryProviderUnavailable as exc:
                self._provider_error = str(exc)

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.settings.enabled and self.settings.provider != "off"

    def status(self) -> dict[str, Any]:
        return {
            "settings": self.settings.to_dict(),
            "scope": {
                "tenant_id": self.scope.tenant_id,
                "user_id": self.scope.user_id,
                "project_id": self.scope.project_id,
            },
            "providers": list(AVAILABLE_PROVIDERS),
            "available": self.registry.status(),
            "store": str(store_path(self.project_root)),
            "error": self._provider_error,
            "count": self.count(),
        }

    def update_settings(self, *, enabled: bool, provider: str, limit: int) -> dict[str, Any]:
        try:
            saved = save_settings(
                self.project_root,
                MemorySettings(enabled=enabled, provider=provider, limit=limit),
            )
        except MemorySettingsError:
            raise
        self.settings = saved
        self._provider_error = ""
        if saved.enabled:
            self.registry.select(self.scope, saved.provider)
        return self.status()

    # ------------------------------------------------------------------
    def _handle(self):
        if not self.enabled:
            raise MemoryProviderUnavailable("Long-term memory is disabled for this workspace")
        return self.registry.for_scope(self.scope)

    def add(self, content: str, *, metadata: dict[str, Any] | None = None) -> MemoryRecord:
        return self._handle().add(self.scope, content, metadata=metadata or {})

    def search(self, query: str, *, limit: int | None = None) -> list[MemoryRecord]:
        if not self.enabled:
            return []
        return self._handle().search(
            self.scope, query, limit=limit or self.settings.limit,
        )

    def delete(self, record_id: str) -> bool:
        if not self.enabled:
            return False
        return self._handle().delete(self.scope, record_id)

    def list(self, *, limit: int | None = None) -> list[MemoryRecord]:
        """Newest records; the SQLite backend treats an empty query as match-all."""
        return self.search("", limit=limit or self.settings.limit)

    def count(self) -> int:
        if not self.enabled:
            return 0
        try:
            return len(self.search("", limit=50))
        except Exception:
            return 0

    @staticmethod
    def record_to_dict(record: MemoryRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "content": record.content,
            "created_at": record.created_at,
            "metadata": dict(record.metadata),
        }
