"""Append-only JSONL session log.

The snapshot file remains the compatibility surface, but every save now also
appends durable events to ``<session_id>.jsonl``. Design rules (borrowed from
DeepSeek Harness' session log):

* the log is append-only; a rewrite is a new generation published atomically;
* readers tolerate a truncated tail from a crashed write;
* model history is *derived* from events rather than stored twice;
* the header carries a schema version so later migrations have an anchor.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

CURRENT_SCHEMA = 2
LOG_SUFFIX = ".jsonl"


@dataclass
class SessionLog:
    directory: Path
    session_id: str

    @property
    def path(self) -> Path:
        return self.directory / f"{self.session_id}{LOG_SUFFIX}"

    def exists(self) -> bool:
        return self.path.exists()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def read_header(self) -> Optional[dict[str, Any]]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                first = handle.readline()
        except OSError:
            return None
        if not first.strip():
            return None
        try:
            record = json.loads(first)
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) and record.get("type") == "header" else None

    def read_events(self) -> list[dict[str, Any]]:
        """Return committed message events; a partial trailing line is ignored."""
        if not self.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        # Only the final line may be a torn write; anything
                        # earlier means the file is corrupt, so stop there.
                        break
                    if index == 0 and record.get("type") == "header":
                        continue
                    if isinstance(record, dict) and record.get("type") == "message":
                        events.append(record)
        except OSError:
            return []
        return events

    def derive_messages(self) -> list[dict[str, Any]]:
        """Project committed events back into the snapshot message shape."""
        messages: list[dict[str, Any]] = []
        for event in self.read_events():
            message: dict[str, Any] = {
                "role": event.get("role", "user"),
                "content": event.get("content", ""),
            }
            for key in ("metadata", "message_id", "parent_id", "reasoning_content"):
                if event.get(key) not in (None, "", {}):
                    message[key] = event[key]
            messages.append(message)
        return messages

    def derive_entries(self) -> list[dict[str, Any]]:
        """Project events into the history-tree entry shape."""
        entries: list[dict[str, Any]] = []
        for event in self.read_events():
            entries.append({
                "type": "message",
                "id": event.get("message_id"),
                "parentId": event.get("parent_id"),
                "role": event.get("role", "user"),
                "content": event.get("content", ""),
                "timestamp": event.get("timestamp", ""),
            })
        return entries

    def to_snapshot(self) -> dict[str, Any]:
        """Minimal snapshot rebuilt from the log (used when a JSON file is lost)."""
        return {
            "version": CURRENT_SCHEMA,
            "schema_version": CURRENT_SCHEMA,
            "system_messages": [],
            "history_messages": self.derive_messages(),
            "history_entries": self.derive_entries(),
            "cursor_id": None,
            "current_model": None,
            "thinking_level": None,
        }

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def sync(self, snapshot: dict[str, Any]) -> int:
        """Bring the log in line with *snapshot*; returns appended event count.

        History that shrank (fork, rollback, manual repair) is republished as a
        fresh generation, because an append-only log cannot drop a suffix.
        """
        messages = snapshot.get("history_messages")
        if not isinstance(messages, list):
            messages = []
        if not self.exists():
            self._publish(messages, snapshot)
            return len(messages)

        existing = self.read_events()
        if len(messages) < len(existing):
            self._publish(messages, snapshot)
            return len(messages)
        if len(messages) == len(existing):
            return 0
        if not self._ends_with_newline():
            # A torn tail cannot be appended to safely; republish instead.
            self._publish(messages, snapshot)
            return len(messages)
        appended = 0
        with self.path.open("a", encoding="utf-8") as handle:
            for offset, message in enumerate(messages[len(existing):], start=len(existing) + 1):
                handle.write(json.dumps(
                    _message_event(self.session_id, offset, message),
                    ensure_ascii=False,
                ) + "\n")
                appended += 1
            handle.flush()
            os.fsync(handle.fileno())
        return appended

    def _ends_with_newline(self) -> bool:
        try:
            with self.path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) == b"\n"
        except (OSError, ValueError):
            return False

    def _publish(self, messages: Iterable[dict[str, Any]], snapshot: dict[str, Any]) -> None:
        """Write a complete generation atomically (header + every message)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        header = {
            "type": "header",
            "schema": CURRENT_SCHEMA,
            "session_id": self.session_id,
            "created_at": snapshot.get("created_at") or "",
        }
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")
                for index, message in enumerate(messages, start=1):
                    handle.write(json.dumps(
                        _message_event(self.session_id, index, message),
                        ensure_ascii=False,
                    ) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)


def _message_event(session_id: str, seq: int, message: dict[str, Any]) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "seq": seq,
        "session_id": session_id,
        "role": message.get("role", "user"),
        "content": message.get("content", ""),
        "timestamp": message.get("timestamp") or "",
    }
    for key in ("metadata", "message_id", "parent_id", "reasoning_content"):
        value = message.get(key)
        if value not in (None, "", {}):
            event[key] = value
    return event
