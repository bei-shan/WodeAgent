"""Replayable Server-Sent Events transport built on the existing session journal.

No additional queues, cursors shared across clients or unbounded buffers.
SSE is opt-in alongside existing WebSocket transport.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from core.events import AgentEvent
from core.runtime.event_journal import EventJournal

_CURSOR = re.compile(r"(?:(?P<epoch>[a-f0-9]{32}):)?(?P<seq>[0-9]{1,18})\Z")


def parse_event_cursor(value: str | None, journal: EventJournal, *, busy: bool) -> tuple[int, str | None]:
    """Return replay cursor and optional reset reason.

    Header values carry a random journal epoch, avoiding undetected replay
    from a previous process in which sequence IDs may have been reused.
    """
    if value is None:
        return (journal.active_turn_cursor() if busy else journal.latest_seq), None
    match = _CURSOR.fullmatch(value)
    if not match:
        raise ValueError("Invalid SSE event cursor")
    seq = int(match.group("seq"))
    epoch = match.group("epoch")
    if epoch is not None and epoch != journal.stream_id:
        return (journal.active_turn_cursor() if busy else journal.latest_seq), "stream_restarted"
    if seq > journal.latest_seq:
        return journal.latest_seq, "cursor_ahead"
    return seq, None


def frame(data: dict[str, Any], journal: EventJournal, seq: int | None = None) -> bytes:
    """JSON-encode in one data line, escaping newlines and control characters."""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    id_line = f"id: {journal.stream_id}:{seq}\n" if seq is not None else ""
    return (id_line + "data: " + body + "\n\n").encode("utf-8")


async def follow_events(
    journal: EventJournal,
    *,
    cursor: int,
    reset_reason: str | None,
    events: set[str],
    sanitize: Callable[[dict], dict],
    disconnected: Callable[[], Awaitable[bool]],
    pending: tuple[AgentEvent, ...] = (),
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[bytes]:
    """Follow bounded replay with gap markers; no lost transition at tail."""
    if reset_reason:
        yield frame({
            "type": "stream.reset", "step": 0,
            "payload": {
                "reason": reset_reason, "resume_from": cursor,
                "oldest_seq": max(0, journal.read_after(cursor).oldest_seq),
                "latest_seq": journal.latest_seq,
            },
        }, journal, cursor)
    for event in pending:
        if event.type in events:
            yield frame({
                "type": event.type, "step": event.step,
                "payload": sanitize(event.payload),
            }, journal)
    last_heartbeat = time.monotonic()
    while not await disconnected():
        batch = await asyncio.to_thread(journal.wait_after, cursor, 0.5, 128)
        if await disconnected():
            break
        if batch.gap:
            cursor = batch.oldest_seq - 1
            yield frame({
                "type": "stream.reset", "step": 0,
                "payload": {
                    "reason": "cursor_expired", "resume_from": cursor,
                    "oldest_seq": batch.oldest_seq,
                    "latest_seq": batch.latest_seq,
                },
            }, journal, cursor)
        for item in batch.events:
            cursor = item.seq
            if item.event.type not in events:
                continue
            yield frame({
                "type": item.event.type,
                "payload": sanitize(item.event.payload),
                "step": item.event.step,
                "seq": cursor,
            }, journal, cursor)
        if time.monotonic() - last_heartbeat >= heartbeat_seconds:
            yield b": keepalive\n\n"
            last_heartbeat = time.monotonic()
