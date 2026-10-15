"""Read-only, bounded E2B reservation/remote inventory audit.

This module *never* adopts, kills or releases a VM. Redis HSCAN is not a
transactional snapshot and remote list results can be stale. Any discrepancy
is an operator investigation hint, never authority to delete a sandbox or
release a capacity reservation.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

from core.sandbox.contracts import SandboxUnavailable

_TOKEN = re.compile(r"[a-f0-9]{48}\Z")
_OWNER = re.compile(r"[a-f0-9]{64}\Z")
_VM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{3,199}\Z")


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


@dataclass(frozen=True)
class E2BAuditReport:
    schema_version: int
    redis_complete: bool
    remote_status: str
    tracked: int
    pending: int
    execution_claims: int
    malformed_records: int
    possible_missing_remote: int | None
    possible_untracked_remote: int | None
    vm_fingerprints: tuple[str, ...]
    note: str = "Read-only indicative inventory; never authority to kill or release VM."

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class E2BReconciliationAuditor:
    """Bounded diagnostics for a single named, trusted Redis deployment."""

    def __init__(
        self, capacity: Any, *, max_pages: int = 3, max_items: int = 128,
        max_seconds: float = 3,
    ):
        if type(max_pages) is not int or not 1 <= max_pages <= 8:
            raise ValueError("E2B audit max_pages out of range")
        if type(max_items) is not int or not 8 <= max_items <= 128:
            raise ValueError("E2B audit max_items out of range")
        if (type(max_seconds) not in (int, float)
                or not 0 < max_seconds <= 10):
            raise ValueError("E2B audit max_seconds out of range")
        for attr in ("_redis", "_key", "_command_key"):
            if not hasattr(capacity, attr):
                raise SandboxUnavailable("E2B audit requires Redis capacity")
        self._redis = capacity._redis
        self._reservations = capacity._key
        self._commands = capacity._command_key
        self._max_pages = max_pages
        self._max_items = max_items
        self._max_seconds = float(max_seconds)

    def _scan(self, key: str, deadline: float) -> tuple[dict[str, str], bool]:
        snapshot: dict[str, str] = {}
        cursor: int | str = 0
        pages = 0
        try:
            while pages < self._max_pages and time.monotonic() < deadline:
                cursor, entries = self._redis.hscan(
                    key, cursor=cursor,
                    count=min(64, self._max_items),
                )
                if not isinstance(entries, dict):
                    raise ValueError("invalid HSCAN response")
                pages += 1
                for k, v in entries.items():
                    if len(snapshot) == self._max_items:
                        return snapshot, False
                    snapshot[_text(k)] = _text(v)
                if int(cursor) == 0:
                    return snapshot, True
        except Exception:
            return snapshot, False
        return snapshot, False

    def _remote_inventory(
        self, remote_inventory: Callable[[], Iterable[Any]], deadline: float,
    ) -> tuple[dict[str, str], bool]:
        outcome: list[tuple[dict[str, str], bool]] = []
        done = threading.Event()

        def worker():
            verified: dict[str, str] = {}
            complete = False
            try:
                entries = remote_inventory()
                complete = True
                for entry in entries:
                    if len(verified) == self._max_items or time.monotonic() >= deadline:
                        complete = False
                        break
                    meta = (entry.get("metadata") if isinstance(entry, dict)
                            else getattr(entry, "metadata", None))
                    vm_id = (entry.get("sandbox_id") if isinstance(entry, dict)
                             else getattr(entry, "sandbox_id", None))
                    if not isinstance(meta, dict):
                        complete = False
                        break
                    session = meta.get("ridge_session")
                    if (meta.get("ridge_provider") != "e2b_experimental"
                            or not isinstance(session, str)
                            or not _TOKEN.fullmatch(session)
                            or not isinstance(vm_id, str)
                            or not _VM.fullmatch(vm_id)):
                        complete = False
                        break
                    verified[session] = vm_id
            except Exception:
                complete = False
            finally:
                outcome.append((verified, complete))
                done.set()

        worker_thread = threading.Thread(
            target=worker, name="ridge-e2b-inventory-audit", daemon=True,
        )
        worker_thread.start()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not done.wait(remaining):
            return {}, False
        return outcome[0]

    def audit(
        self, remote_inventory: Callable[[], Iterable[Any]] | None = None,
    ) -> E2BAuditReport:
        deadline = time.monotonic() + self._max_seconds
        reservations, complete1 = self._scan(self._reservations, deadline)
        commands, complete2 = self._scan(self._commands, deadline)
        valid: dict[str, str] = {}
        pending = 0
        malformed = 0
        for token, raw in reservations.items():
            owner, sep, remote_id = raw.partition(":")
            if not _TOKEN.fullmatch(token) or not sep or not _OWNER.fullmatch(owner):
                malformed += 1
                continue
            if remote_id == "pending":
                pending += 1
            elif _VM.fullmatch(remote_id):
                valid[token] = remote_id
            else:
                malformed += 1
        claims = sum(1 for token, value in commands.items() if
                     token in reservations and isinstance(value, str))
        remote_status = "not_checked"
        missing = unknown = None
        if remote_inventory is not None:
            reported, complete3 = self._remote_inventory(remote_inventory, deadline)
            remote_status = "complete" if complete3 else "incomplete"
            # A Redis HSCAN snapshot is not transactional; discrepancies may
            # reflect concurrent activity. Never auto-reconcile from numbers.
            if complete1 and complete2 and complete3:
                missing = sum(reported.get(k) != v for k, v in valid.items())
                unknown = sum(valid.get(k) != v for k, v in reported.items())
        fingerprints = tuple(sorted(_short_hash(x) for x in valid.values())[:32])
        return E2BAuditReport(
            schema_version=1,
            redis_complete=complete1 and complete2,
            remote_status=remote_status,
            tracked=len(valid), pending=pending,
            execution_claims=claims, malformed_records=malformed,
            possible_missing_remote=missing,
            possible_untracked_remote=unknown,
            vm_fingerprints=fingerprints,
        )
