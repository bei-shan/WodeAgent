"""Opt-in Mem0 OSS/SDK adapter with server-derived opaque scope IDs.

No automatic recording or use of model-generated memory. A caller must
explicitly approve transmission to the configured Mem0 service, and must
supply a trusted client. This is NOT a generic caller-selected tenant filter.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Mapping

from .contracts import MemoryRecord, MemoryScope


class Mem0MemoryBackend:
    """Exact-scope Mem0 adapter; never trusts remote search filtering alone.

    The Mem0 database must itself be protected by tenant-scoped credentials
    or trusted infrastructure. For shared OSS deployments, the HMAC-derived
    synthetic user ID prevents raw tenant names entering Mem0 and separates
    dimensions; it is not a cryptographic substitute for backend ACL.
    """

    def __init__(self, client: Any, *, scope_key: bytes, approved: bool = False):
        if not approved:
            raise PermissionError("Mem0 content transmission requires explicit opt-in")
        if not isinstance(scope_key, bytes) or len(scope_key) < 32:
            raise ValueError("A persistent 32-byte or stronger scope key is required")
        if not all(callable(getattr(client, name, None)) for name in ("add", "search", "get", "delete")):
            raise TypeError("A configured Mem0 SDK client is required")
        self._client = client
        self._key = scope_key

    def _user_key(self, scope: MemoryScope) -> str:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Verified MemoryScope required")
        encoded = json.dumps(
            [scope.tenant_id, scope.user_id, scope.project_id, scope.agent_id],
            separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        return "ridge_" + hmac.new(self._key, encoded, hashlib.sha256).hexdigest()

    @staticmethod
    def _record(item: Mapping[str, Any], scope: MemoryScope) -> MemoryRecord:
        rid = item.get("id")
        content = item.get("memory")
        if not isinstance(rid, str) or not rid or not isinstance(content, str):
            raise ValueError("Mem0 returned an invalid memory item")
        stamp = item.get("created_at")
        if not isinstance(stamp, str) or not stamp:
            stamp = datetime.now(timezone.utc).isoformat()
        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        return MemoryRecord(
            id=rid, scope=scope, content=content,
            created_at=stamp,
            metadata={k: v for k, v in metadata.items()
                      if k not in {"user_id", "tenant_id", "project_id", "agent_id"}},
        )

    def add(
        self, scope: MemoryScope, content: str, *,
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryRecord:
        owner = self._user_key(scope)
        if not isinstance(content, str) or not 0 < len(content.strip()) <= 20000:
            raise ValueError("Memory content must be 1..20000 characters")
        extras = dict(metadata or {})
        for reserved in ("user_id", "tenant_id", "project_id", "agent_id", "run_id"):
            if reserved in extras:
                raise ValueError("Memory metadata cannot override ownership")
        # Explicit infer=False: no surprise extraction, mutation, or extra
        # LLM call when the user asked only to save a memory.
        response = self._client.add(
            content.strip(), user_id=owner, infer=False, metadata=extras,
        )
        records = response.get("results", []) if isinstance(response, dict) else []
        if len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError("Mem0 did not confirm a single saved memory")
        saved = records[0]
        if saved.get("user_id") != owner:
            # Many SDK versions omit the owner from an add() result.
            # Verify persistence with get() rather than assuming ownership.
            saved = self._client.get(saved.get("id"))
        if not isinstance(saved, dict) or saved.get("user_id") != owner:
            raise ValueError("Mem0 could not verify persisted memory ownership")
        return self._record(saved, scope)

    def search(self, scope: MemoryScope, query: str, *, limit: int = 8) -> list[MemoryRecord]:
        owner = self._user_key(scope)
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 2048:
            raise ValueError("Invalid memory query")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Search limit must be 1..50")
        response = self._client.search(
            query.strip(), filters={"user_id": owner}, top_k=limit,
        )
        records = response.get("results", []) if isinstance(response, dict) else []
        if not isinstance(records, list):
            raise ValueError("Invalid Mem0 results")
        # Defense in depth: every returned row must explicitly affirm scope.
        return [
            self._record(item, scope)
            for item in records[:limit]
            if isinstance(item, dict) and item.get("user_id") == owner
        ]

    def delete(self, scope: MemoryScope, record_id: str) -> bool:
        owner = self._user_key(scope)
        if not isinstance(record_id, str) or not 0 < len(record_id) <= 128:
            return False
        record = self._client.get(record_id)
        if not isinstance(record, dict) or record.get("user_id") != owner:
            return False
        self._client.delete(record_id)
        return True
