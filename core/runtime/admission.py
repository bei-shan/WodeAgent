"""Bounded, non-blocking turn admission for the local single-process runtime.

The API never accepts a caller-supplied owner identifier. Future hosted mode
must derive owner keys from a *verified* principal and replace process-local
counters with distributed atomic leases; this module is not tenant auth.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


class TurnCapacityExceeded(RuntimeError):
    """Reject work immediately rather than launching unbounded worker threads."""


@dataclass(frozen=True)
class AdmissionSnapshot:
    active: int
    limit: int
    rejected: int


class TurnPermit:
    """Idempotent permit released by the worker's terminal finally block."""

    def __init__(self, admission: "TurnAdmission", owner: str):
        self._admission = admission
        self._owner = owner
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            self._admission._release(self._owner)


class TurnAdmission:
    """Thread-safe fail-fast global and per-owner running-turn limits.

    Does not retain a task queue (no unbounded waiters, prompt history or user
    data); a caller can retry after receiving 429. Counts hold while an Agent
    waits on an interactive permission prompt or a slow provider.
    """

    def __init__(self, *, max_active: int = 8, max_per_owner: int | None = None):
        if type(max_active) is not int or not 1 <= max_active <= 256:
            raise ValueError("max_active must be an integer between 1 and 256")
        if max_per_owner is None:
            max_per_owner = max_active
        if type(max_per_owner) is not int or not 1 <= max_per_owner <= max_active:
            raise ValueError("max_per_owner must be between 1 and max_active")
        self._limit = max_active
        self._per_owner_limit = max_per_owner
        self._active = 0
        self._rejected = 0
        self._owners: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, owner: str) -> TurnPermit:
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise ValueError("A server-derived owner key is required")
        with self._lock:
            if self._active >= self._limit or self._owners.get(owner, 0) >= self._per_owner_limit:
                self._rejected += 1
                raise TurnCapacityExceeded("Agent turn capacity exhausted; retry later")
            self._active += 1
            self._owners[owner] = self._owners.get(owner, 0) + 1
        return TurnPermit(self, owner)

    def _release(self, owner: str) -> None:
        with self._lock:
            count = self._owners[owner]
            if count == 1:
                del self._owners[owner]
            else:
                self._owners[owner] = count - 1
            self._active -= 1

    def snapshot(self) -> AdmissionSnapshot:
        with self._lock:
            return AdmissionSnapshot(self._active, self._limit, self._rejected)
