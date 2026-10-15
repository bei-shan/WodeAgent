"""Session-scoped warm BoxLite micro-VM pool with bounded local ownership.

All BoxLite async handles are created, used and destroyed on one private event
loop, like DeerFlow's BoxLite provider. No VM is ever reassigned across scopes:
warm reuse is only for exactly the same opaque session identity. There is no
cross-process lease or ownership adoption in this local experimental phase.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from core.sandbox.contracts import CommandResult, SandboxUnavailable

T = TypeVar("T")


@dataclass
class _BoxLease:
    box: Any
    active: bool
    last_used: float
    executing: bool = False


class BoxliteSessionPool:
    """Hard capped pool; fail closed on capacity exhaustion or identity collision."""

    def __init__(
        self,
        box_factory: Callable[[], Any],
        *,
        max_replicas: int = 2,
        idle_timeout: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(max_replicas) is not int or not 1 <= max_replicas <= 8:
            raise ValueError("max_replicas must be an integer from 1 to 8")
        if (isinstance(idle_timeout, bool) or not isinstance(idle_timeout, (int, float))
                or not 0 <= idle_timeout <= 3600):
            raise ValueError("idle_timeout must be between 0 and 3600s")
        self._factory = box_factory
        self._capacity = max_replicas
        self._idle_timeout = idle_timeout
        self._clock = clock
        self._leases: dict[str, _BoxLease] = {}
        self._pending: set[str] = set()
        self._lock = threading.RLock()
        self._closed = False
        self._poisoned = False  # uncertain SDK disposal must not free capacity
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._loop_main, name="ridge-boxlite-vm-loop", daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise SandboxUnavailable("BoxLite event loop did not initialize")
        self._stop_reaper = threading.Event()
        self._reaper = threading.Thread(
            target=self._reaper_main, name="ridge-boxlite-vm-reaper", daemon=True,
        )
        self._reaper.start()

    def _loop_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        # The loop thread owns closure, including remaining async tasks.
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def _invoke(self, coro: Awaitable[T], timeout: float = 15) -> T:
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as exc:
            coro.close()
            raise SandboxUnavailable("BoxLite runtime is closed") from exc
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise SandboxUnavailable("BoxLite operation exceeded its deadline") from exc

    async def _start_box(self) -> Any:
        box = self._factory()
        try:
            await box.__aenter__()
        except BaseException:
            # Failed initializations may have allocated SDK resources.
            try:
                await box.__aexit__(None, None, None)
            except Exception:
                pass
            raise
        return box

    async def _stop_box(self, box: Any) -> None:
        await box.__aexit__(None, None, None)

    def _dispose(self, box: Any) -> bool:
        try:
            self._invoke(self._stop_box(box), timeout=10)
            return True
        except Exception:
            # An unconfirmed VM shutdown may still consume host resources.
            # Never admit new guests on this pool until operator restart.
            with self._lock:
                self._poisoned = True
            return False

    def acquire(self, scope: str) -> str:
        if not isinstance(scope, str) or not 1 <= len(scope) <= 128 or "\x00" in scope:
            raise ValueError("Session scope must be an opaque nonempty identifier")
        evicted: Any = None
        with self._lock:
            if self._closed or self._poisoned:
                raise SandboxUnavailable("BoxLite pool unavailable or cleanup unconfirmed")
            lease = self._leases.get(scope)
            if lease is not None:
                if lease.active:
                    raise SandboxUnavailable("Sandbox session is already acquired")
                lease.active = True
                return scope
            if scope in self._pending:
                raise SandboxUnavailable("Sandbox session creation already in progress")
            if len(self._leases) + len(self._pending) >= self._capacity:
                warm = [
                    (key, value) for key, value in self._leases.items()
                    if not value.active and not value.executing
                ]
                if not warm:
                    raise SandboxUnavailable("BoxLite replica capacity exceeded")
                victim, unused = min(warm, key=lambda item: item[1].last_used)
                evicted = self._leases.pop(victim).box
            self._pending.add(scope)
        if evicted is not None and not self._dispose(evicted):
            with self._lock:
                self._pending.discard(scope)
            raise SandboxUnavailable("Old VM cleanup unconfirmed; capacity cannot be reused")
        box = None
        try:
            box = self._invoke(self._start_box(), timeout=90)
            with self._lock:
                if self._closed:
                    raise SandboxUnavailable("BoxLite pool closed during guest startup")
                self._leases[scope] = _BoxLease(box, True, self._clock())
                return scope
        except Exception as exc:
            if box is not None:
                self._dispose(box)
            if isinstance(exc, SandboxUnavailable):
                raise
            raise SandboxUnavailable("Unable to acquire BoxLite VM; no host fallback") from exc
        finally:
            with self._lock:
                self._pending.discard(scope)

    def execute(
        self,
        scope: str,
        executor: Callable[[Any], Awaitable[CommandResult]],
        *,
        timeout_seconds: float,
    ) -> CommandResult:
        with self._lock:
            lease = self._leases.get(scope)
            if self._closed or self._poisoned or lease is None or not lease.active or lease.executing:
                raise SandboxUnavailable("No exclusive active VM lease for this session")
            lease.executing = True
            box = lease.box
        invalidate = False
        try:
            try:
                result = self._invoke(executor(box), timeout=timeout_seconds + 5)
            except Exception as exc:
                invalidate = True
                raise SandboxUnavailable("BoxLite guest command failed; host fallback forbidden") from exc
            invalidate = result.timed_out or result.interrupted
            return result
        finally:
            with self._lock:
                lease.executing = False
                if invalidate and self._leases.get(scope) is lease:
                    self._leases.pop(scope, None)
            if invalidate:
                self._dispose(box)

    def release(self, scope: str) -> None:
        evicted: Any = None
        with self._lock:
            lease = self._leases.get(scope)
            if lease is None:
                return
            if lease.executing:
                raise SandboxUnavailable("Cannot release an executing VM lease")
            lease.active = False
            lease.last_used = self._clock()
            if self._idle_timeout == 0:
                evicted = self._leases.pop(scope).box
        if evicted is not None:
            self._dispose(evicted)

    def reap_idle(self) -> int:
        removed: list[Any] = []
        with self._lock:
            if self._closed:
                return 0
            for scope, lease in list(self._leases.items()):
                if (not lease.active and not lease.executing
                        and self._clock() - lease.last_used >= self._idle_timeout):
                    removed.append(self._leases.pop(scope).box)
        for box in removed:
            self._dispose(box)
        return len(removed)

    def _reaper_main(self) -> None:
        while not self._stop_reaper.wait(5):
            self.reap_idle()

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            boxes = [entry.box for entry in self._leases.values()]
            self._leases.clear()
        self._stop_reaper.set()
        self._reaper.join(timeout=6)
        for box in boxes:
            self._dispose(box)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=6)

    @property
    def counts(self) -> tuple[int, int, int]:
        """active, warm and in-progress count; excludes scope identifiers."""
        with self._lock:
            return (
                sum(x.active for x in self._leases.values()),
                sum(not x.active for x in self._leases.values()),
                len(self._pending),
            )
