"""Strict, backend-neutral long-term memory contract.

A MemoryScope must be constructed from an authenticated server principal, NEVER
from untrusted JSON, a model tool call or arbitrary request headers. This module
is a foundation, not an authorization system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Private owner scope; team-shared memory needs a separate ACL contract.

    Every dimension participates in *every* read, write and delete. IDs are
    opaque server-issued identifiers, not filesystem paths or display names.
    """

    tenant_id: str
    user_id: str
    project_id: str
    agent_id: str = "default"

    def __post_init__(self) -> None:
        for name in ("tenant_id", "user_id", "project_id", "agent_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError(f"Invalid or missing memory scope: {name}")


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    scope: MemoryScope
    content: str
    created_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class MemoryBackend(Protocol):
    """Sync API: call it from an Agent worker or asyncio.to_thread, not ASGI."""

    def add(
        self,
        scope: MemoryScope,
        content: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryRecord: ...

    def search(
        self, scope: MemoryScope, query: str, *, limit: int = 8,
    ) -> list[MemoryRecord]: ...

    def delete(self, scope: MemoryScope, record_id: str) -> bool: ...
