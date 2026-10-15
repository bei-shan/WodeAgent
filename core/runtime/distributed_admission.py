"""Atomic Redis global/tenant/user/project concurrency leases.

Standalone infrastructure primitive: not connected to the local single-user
SessionController or enabled for untrusted hosted Agent execution.
Production callers MUST derive scopes from verified OIDC membership and
cancel work if lease renewal fails. Redis outages are fail-closed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from typing import Any

from core.memory.contracts import MemoryScope


class DistributedTurnCapacityExceeded(RuntimeError):
    pass


class DistributedAdmissionUnavailable(RuntimeError):
    pass


_ACQUIRE = """
local tm=redis.call('TIME')
local now=tonumber(tm[1])*1000+math.floor(tonumber(tm[2])/1000)
local ttl=tonumber(ARGV[2])
for i=1,4 do
  redis.call('ZREMRANGEBYSCORE',KEYS[i],'-inf',now)
  if redis.call('ZCARD',KEYS[i]) >= tonumber(ARGV[i+2]) then
    return 0
  end
end
for i=1,4 do
  redis.call('ZADD',KEYS[i],now+ttl,ARGV[1])
  redis.call('PEXPIRE',KEYS[i],ttl*2)
end
return 1
"""

_RENEW = """
local tm=redis.call('TIME')
local now=tonumber(tm[1])*1000+math.floor(tonumber(tm[2])/1000)
for i=1,4 do
  local score=redis.call('ZSCORE',KEYS[i],ARGV[1])
  if not score or tonumber(score)<=now then return 0 end
end
local ttl=tonumber(ARGV[2])
for i=1,4 do
  redis.call('ZADD',KEYS[i],now+ttl,ARGV[1])
  redis.call('PEXPIRE',KEYS[i],ttl*2)
end
return 1
"""

_RELEASE = """
for i=1,4 do redis.call('ZREM',KEYS[i],ARGV[1]) end
return 1
"""


class RedisTurnPermit:
    def __init__(self, owner: "RedisTurnAdmission", keys: tuple[str, ...], token: str):
        self._owner = owner
        self._keys = keys
        self._token = token
        self._lock = threading.Lock()
        self._released = False

    def renew(self) -> bool:
        with self._lock:
            if self._released:
                return False
            return self._owner._call(_RENEW, self._keys, self._token,
                                     self._owner.ttl_ms) == 1

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            # Do not mark released on a failed network operation. The lease
            # still expires by Redis server time, but callers can retry.
            self._owner._call(_RELEASE, self._keys, self._token)
            self._released = True


class RedisTurnAdmission:
    """Distributed atomic leases using a shared, authenticated Redis service.

    All four ZSET keys deliberately use one Redis Cluster hash tag for Lua
    atomicity. This keeps one deployment's quota ledger on a single shard;
    capacity planning must account for that hot key.
    """

    def __init__(
        self, client: Any, *, hmac_key: bytes,
        namespace: str = "ridge-turns-v1",
        max_global: int = 64, max_tenant: int = 16,
        max_user: int = 8, max_project: int = 16,
        ttl_ms: int = 30000,
    ):
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise ValueError("Stable HMAC key (>=32 bytes) required")
        if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", namespace):
            raise ValueError("Invalid Redis quota namespace")
        limits = (max_global, max_tenant, max_user, max_project)
        if any(type(x) is not int or not 1 <= x <= 10000 for x in limits):
            raise ValueError("Capacity bounds must be positive integers <=10000")
        if (max_tenant > max_global or max_user > max_tenant
                or max_project > max_tenant):
            raise ValueError("Capacity limits must be hierarchical")
        if type(ttl_ms) is not int or not 5000 <= ttl_ms <= 120000:
            raise ValueError("Lease TTL must be 5000..120000 ms")
        if not callable(getattr(client, "eval", None)):
            raise TypeError("Configured Redis client with EVAL required")
        self._client = client
        self._key = hmac_key
        self._prefix = "{" + namespace + "}:turns"
        self._limits = limits
        self.ttl_ms = ttl_ms

    def _digest(self, values: list[str]) -> str:
        raw = json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode()
        return hmac.new(self._key, raw, hashlib.sha256).hexdigest()

    def _keys(self, scope: MemoryScope) -> tuple[str, ...]:
        if not isinstance(scope, MemoryScope):
            raise TypeError("Server-verified MemoryScope required")
        t = self._digest([scope.tenant_id])
        u = self._digest([scope.tenant_id, scope.user_id])
        p = self._digest([scope.tenant_id, scope.project_id])
        return (self._prefix+":global", self._prefix+":tenant:"+t,
                self._prefix+":user:"+u, self._prefix+":project:"+p)

    def _call(self, script: str, keys: tuple[str, ...], *args: Any) -> int:
        try:
            return int(self._client.eval(script, len(keys), *keys, *args))
        except Exception as exc:
            raise DistributedAdmissionUnavailable(
                "Shared admission store unavailable; rejecting work"
            ) from exc

    def acquire(self, scope: MemoryScope) -> RedisTurnPermit:
        keys = self._keys(scope)
        token = secrets.token_hex(24)
        if self._call(_ACQUIRE, keys, token, self.ttl_ms, *self._limits) != 1:
            raise DistributedTurnCapacityExceeded("Distributed Agent capacity exhausted")
        return RedisTurnPermit(self, keys, token)
