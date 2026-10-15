"""Redis-atomic, non-expiring E2B VM capacity reservations.

This is a *capacity* and owner-token ledger, not a full remote-VM discovery
or distributed session ownership service. Durable reservations deliberately
never expire automatically: an unreachable but live VM must not silently free
an allocation. Operators reconcile only after verifying remote destruction.

Redis scripts are authoritative. No fallback to a process-local cap on Redis
failure, Redis timeout or configuration mismatch.
"""
from __future__ import annotations

import re
import secrets
from typing import Any

from core.sandbox.contracts import SandboxUnavailable

_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_VM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{3,199}\Z")
_TOKEN = re.compile(r"[a-f0-9]{48}\Z")

_RESERVE = """
local previous = redis.call('GET', KEYS[2])
if previous ~= false and previous ~= ARGV[3] then return 'CONFIG_MISMATCH' end
if previous == false then
  redis.call('SETNX', KEYS[2], ARGV[3])
  if redis.call('GET', KEYS[2]) ~= ARGV[3] then return 'CONFIG_MISMATCH' end
end
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 'DUPLICATE' end
if redis.call('HLEN', KEYS[1]) >= tonumber(ARGV[3]) then return 'FULL' end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2] .. ':pending')
return 'GRANTED'
"""

_TRACK = """
local entry = redis.call('HGET', KEYS[1], ARGV[1])
if entry == false then return 'MISSING' end
if entry == ARGV[2] .. ':pending' or entry == ARGV[2] .. ':' .. ARGV[3] then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2] .. ':' .. ARGV[3])
  return 'TRACKED'
end
return 'FENCED'
"""

_RELEASE = """
local entry = redis.call('HGET', KEYS[1], ARGV[1])
if entry == false then return 'MISSING' end
if string.sub(entry, 1, string.len(ARGV[2]) + 1) ~= ARGV[2] .. ':' then
  return 'FENCED'
end
redis.call('HDEL', KEYS[1], ARGV[1])
return 'RELEASED'
"""


class RedisE2BCapacity:
    """Atomic capacity admission and fenced release for trusted Redis clients."""

    def __init__(
        self, *,
        deployment: str,
        limit: int,
        redis_url: str | None = None,
        redis_client: Any = None,
    ):
        if not isinstance(deployment, str) or not _SCOPE.fullmatch(deployment):
            raise ValueError("E2B Redis deployment scope is invalid")
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("E2B Redis capacity must be 1–16")
        if redis_client is None:
            if not isinstance(redis_url, str) or not redis_url.startswith(
                ("redis://", "rediss://")
            ):
                raise SandboxUnavailable("E2B Redis URL must be configured")
            try:
                from redis import Redis
            except ImportError:
                raise SandboxUnavailable("Optional redis Python SDK is missing") from None
            redis_client = Redis.from_url(
                redis_url, decode_responses=True,
                socket_timeout=3, socket_connect_timeout=3,
                retry_on_timeout=False,
            )
        self._redis = redis_client
        self._owner = secrets.token_hex(32)
        self.limit = limit
        self._key = f"ridgecode:e2b:{deployment}:reservations"
        self._limit_key = f"ridgecode:e2b:{deployment}:limit"

    def _eval(self, script: str, token: str, *args: object) -> str:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid E2B reservation identity")
        try:
            result = self._redis.eval(
                script, 2, self._key, self._limit_key,
                token, self._owner, *args,
            )
        except Exception:
            # Never include the Redis URL, auth or raw server response.
            raise SandboxUnavailable("E2B Redis ledger is unavailable") from None
        return result.decode() if isinstance(result, bytes) else str(result)

    def reserve(self, token: str) -> None:
        outcome = self._eval(_RESERVE, token, str(self.limit))
        if outcome == "FULL":
            raise SandboxUnavailable("E2B shared VM capacity exhausted")
        if outcome != "GRANTED":
            raise SandboxUnavailable("E2B Redis ownership or quota is not confirmed")

    def track(self, token: str, sandbox_id: str) -> None:
        if not isinstance(sandbox_id, str) or not _VM_ID.fullmatch(sandbox_id):
            raise SandboxUnavailable("Invalid remote E2B sandbox identity")
        if self._eval(_TRACK, token, sandbox_id) != "TRACKED":
            raise SandboxUnavailable("E2B remote ownership claim was fenced")

    def release(self, token: str) -> None:
        # Only call after a definitive remote kill, never merely after a local
        # command timeout or a lost SDK connection.
        if self._eval(_RELEASE, token) != "RELEASED":
            raise SandboxUnavailable("E2B capacity release was not confirmed")

    @property
    def used(self) -> int:
        try:
            value = self._redis.hlen(self._key)
            return int(value)
        except Exception:
            raise SandboxUnavailable("E2B capacity cannot be verified") from None
