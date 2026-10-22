"""Append-only JSONL session log.

The snapshot file remains the compatibility surface, but every save also
appends durable events to ``<session_id>.jsonl``. Design rules (borrowed from
DeepSeek Harness' session log):

* the log is append-only; a rewrite is a new generation published atomically;
* a sidecar cursor keeps appends O(new events) instead of O(history);
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
STATE_SUFFIX = ".logstate.json"


@dataclass
class SessionLog:
    directory: Path
    session_id: str

    @property
    def path(self) -> Path:
        return self.directory / f"{self.session_id}{LOG_SUFFIX}"

    @property
    def state_path(self) -> Path:
        return self.directory / f"{self.session_id}{STATE_SUFFIX}"

    def exists(self) -> bool:
        return self.path.exists()

    def has_events(self) -> bool:
        """True when the log carries any committed record beyond its header."""
        for record in self._iter_records():
            if record.get("type") in {"message", "branch"}:
                return True
        return False

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

    def _iter_records(self) -> Iterable[dict[str, Any]]:
        """Yield committed records; a torn final line stops the iteration."""
        if not self.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        break
                    if index == 0 and record.get("type") == "header":
                        continue
                    if isinstance(record, dict):
                        yield record
        except OSError:
            return

    def read_events(self) -> list[dict[str, Any]]:
        return [record for record in self._iter_records() if record.get("type") == "message"]

    def read_cursor(self) -> Optional[str]:
        cursor: Optional[str] = None
        for record in self._iter_records():
            if record.get("type") == "cursor":
                value = record.get("value")
                cursor = str(value) if value not in (None, "") else None
        return cursor

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
            "cursor_id": self.read_cursor(),
            "current_model": None,
            "thinking_level": None,
        }

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def _load_state(self) -> Optional[dict[str, Any]]:
        """Append cursor; only trusted while it matches the log's size."""
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            size = self.path.stat().st_size
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("size") != size:
            return None
        events = raw.get("events")
        if not isinstance(events, int) or events < 0:
            return None
        return raw

    def _save_state(self, events: int, cursor: Optional[str]) -> None:
        try:
            payload = {
                "size": self.path.stat().st_size,
                "events": events,
                "cursor": cursor,
                "schema": CURRENT_SCHEMA,
            }
        except OSError:
            return
        tmp = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass
        finally:
            tmp.unlink(missing_ok=True)

    def sync(self, snapshot: dict[str, Any]) -> int:
        """Bring the log in line with *snapshot*; returns appended message count.

        History that shrank (fork, rollback, manual repair) is republished as a
        fresh generation, because an append-only log cannot drop a suffix.
        """
        messages = snapshot.get("history_messages")
        if not isinstance(messages, list):
            messages = []
        cursor = snapshot.get("cursor_id") or None

        if not self.exists():
            self._publish(messages, snapshot, cursor)
            self._save_state(len(messages), cursor)
            return len(messages)

        state = self._load_state()
        if state is None:
            existing = self.read_events()
            if len(messages) < len(existing):
                self._republish_with_branch(messages, snapshot, cursor)
                self._save_state(len(messages), cursor)
                return len(messages)
            if not self._ends_with_newline():
                self._publish(messages, snapshot, cursor)
                self._save_state(len(messages), cursor)
                return len(messages)
            start = len(existing)
            last_cursor = self.read_cursor()
        else:
            start = int(state["events"])
            last_cursor = state.get("cursor")
            if len(messages) < start:
                self._republish_with_branch(messages, snapshot, cursor)
                self._save_state(len(messages), cursor)
                return len(messages)

        appended = 0
        if len(messages) > start:
            appended = self._append_messages(messages[start:], start)
        if cursor != last_cursor:
            self._append_cursor(cursor, len(messages))
        self._save_state(len(messages), cursor)
        return appended

    def rewrite(self, snapshot: dict[str, Any]) -> None:
        """Republish the whole log from *snapshot* (used by migrations)."""
        messages = snapshot.get("history_messages")
        if not isinstance(messages, list):
            messages = []
        cursor = snapshot.get("cursor_id") or None
        self._publish(messages, snapshot, cursor)
        self._save_state(len(messages), cursor)

    def _republish_with_branch(
        self,
        messages: list[dict[str, Any]],
        snapshot: dict[str, Any],
        cursor: Optional[str],
    ) -> None:
        """Republish after a shrink, recording where the branch left the old tail."""
        self._publish(messages, snapshot, cursor, branch=self._branch_record(messages))

    def _branch_record(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        existing = self.derive_messages()
        common = 0
        for left, right in zip(existing, messages):
            left_id, right_id = left.get("message_id"), right.get("message_id")
            if left_id and right_id:
                if left_id != right_id:
                    break
            elif left.get("content") != right.get("content"):
                break
            common += 1
        from_id = ""
        if common:
            from_id = str(existing[common - 1].get("message_id") or "")
        return {
            "from": from_id,
            "from_index": common,
            "from_length": len(existing),
            "to_length": len(messages),
        }

    def read_branches(self) -> list[dict[str, Any]]:
        """Branch points recorded when history shrank (fork/rollback audit)."""
        return [record for record in self._iter_records() if record.get("type") == "branch"]

    def _append_messages(self, messages: Iterable[dict[str, Any]], start: int) -> int:
        appended = 0
        with self.path.open("a", encoding="utf-8") as handle:
            for offset, message in enumerate(messages, start=start + 1):
                handle.write(json.dumps(
                    _message_event(self.session_id, offset, message),
                    ensure_ascii=False,
                ) + "\n")
                appended += 1
            handle.flush()
            os.fsync(handle.fileno())
        return appended

    def _append_cursor(self, cursor: Optional[str], seq: int) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "cursor",
                "seq": seq,
                "session_id": self.session_id,
                "value": cursor,
            }, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _ends_with_newline(self) -> bool:
        try:
            with self.path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) == b"\n"
        except (OSError, ValueError):
            return False

    def _publish(
        self,
        messages: Iterable[dict[str, Any]],
        snapshot: dict[str, Any],
        cursor: Optional[str] = None,
        branch: Optional[dict[str, Any]] = None,
    ) -> None:
        """Write a complete generation atomically (header + branch + messages + cursor)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        header = {
            "type": "header",
            "schema": CURRENT_SCHEMA,
            "session_id": self.session_id,
            "created_at": snapshot.get("created_at") or "",
        }
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        count = 0
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")
                if branch:
                    handle.write(json.dumps({
                        "type": "branch",
                        "session_id": self.session_id,
                        **branch,
                    }, ensure_ascii=False) + "\n")
                for index, message in enumerate(messages, start=1):
                    handle.write(json.dumps(
                        _message_event(self.session_id, index, message),
                        ensure_ascii=False,
                    ) + "\n")
                    count += 1
                if cursor:
                    handle.write(json.dumps({
                        "type": "cursor",
                        "seq": count,
                        "session_id": self.session_id,
                        "value": cursor,
                    }, ensure_ascii=False) + "\n")
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
