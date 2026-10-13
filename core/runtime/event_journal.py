"""Per-session, bounded event journal with replayable monotonic cursors.

Inspired by the durable-log/live-subscription distinction in OpenCode.
This implementation is deliberately *in-memory*: agent conversation snapshots
remain the source of truth across process restarts. Every subscriber uses its
own sequence cursor, so separate WebSocket clients never steal each other's
messages. A bounded ring prevents unbounded token-stream memory growth.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass

from core.events import AgentEvent


@dataclass(frozen=True)
class SequencedEvent:
    seq: int
    event: AgentEvent


@dataclass(frozen=True)
class JournalBatch:
    events: tuple[SequencedEvent, ...]
    gap: bool
    oldest_seq: int
    latest_seq: int


class EventJournal:
    """Thread-safe append/replay/follow journal.

    The legacy get/get_nowait methods retain support for one queue-like
    consumer, while stream followers independently use wait_after().
    """

    def __init__(self, capacity: int = 2048) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._entries: deque[SequencedEvent] = deque(maxlen=capacity)
        self._condition = threading.Condition()
        self._latest = 0
        self._legacy_cursor = 0

    @property
    def latest_seq(self) -> int:
        with self._condition:
            return self._latest

    def put(self, event: AgentEvent, block: bool = True, timeout: float | None = None) -> int:
        """Append without blocking producers; retained args match queue.put."""
        del block, timeout
        with self._condition:
            self._latest += 1
            self._entries.append(SequencedEvent(self._latest, event))
            self._condition.notify_all()
            return self._latest

    def put_nowait(self, event: AgentEvent) -> int:
        return self.put(event)

    def _read_unlocked(self, after: int, limit: int) -> JournalBatch:
        oldest = self._entries[0].seq if self._entries else self._latest + 1
        gap = after < oldest - 1
        visible = tuple(x for x in self._entries if x.seq > after)[:limit]
        return JournalBatch(visible, gap, oldest, self._latest)

    def read_after(self, after: int, limit: int = 128) -> JournalBatch:
        if after < 0 or limit < 1:
            raise ValueError("after must be >= 0 and limit >= 1")
        with self._condition:
            return self._read_unlocked(after, limit)

    def wait_after(self, after: int, timeout: float = 0.5, limit: int = 128) -> JournalBatch:
        if after < 0 or limit < 1:
            raise ValueError("after must be >= 0 and limit >= 1")
        with self._condition:
            batch = self._read_unlocked(after, limit)
            if batch.events or batch.gap or timeout <= 0:
                return batch
            self._condition.wait(timeout=timeout)
            return self._read_unlocked(after, limit)

    def get(self, block: bool = True, timeout: float | None = None) -> AgentEvent:
        """Compatibility queue reader; does not affect stream subscribers."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                batch = self._read_unlocked(self._legacy_cursor, 1)
                if batch.events:
                    item = batch.events[0]
                    self._legacy_cursor = item.seq
                    return item.event
                if not block:
                    raise queue.Empty
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(timeout=remaining)

    def get_nowait(self) -> AgentEvent:
        return self.get(block=False)

    def empty(self) -> bool:
        with self._condition:
            return not self._read_unlocked(self._legacy_cursor, 1).events
