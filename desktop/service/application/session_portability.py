"""Safe, deliberately lossy portable conversation tree archive.

This is not the internal Agent snapshot format. It contains *only* visible
user/assistant messages, tree ancestry, labels and a title. Never emit tool
metadata, summaries, LLM config, environment, system messages or task state.
"""
from __future__ import annotations

import json
import re
import secrets
from typing import Any

from .errors import InvalidInputError, PayloadTooLargeError

FORMAT = "ridgecode.portable-session"
VERSION = 1
MAX_BYTES = 1_048_576
MAX_MESSAGES = 500
MAX_CONTENT_CHARS = 4_096
MAX_TITLE_CHARS = 120
MAX_LABEL_CHARS = 80
_ID = re.compile(r"[0-9a-f]{8}\Z")
_ROLES = frozenset(("user", "assistant"))
_TOP_KEYS = frozenset(("format", "version", "title", "cursor_id", "messages", "labels"))
_MESSAGE_KEYS = frozenset(("id", "parent_id", "role", "content"))


def _invalid(message: str) -> None:
    raise InvalidInputError("Invalid portable session: " + message)


def _within_size(payload: Any) -> None:
    try:
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidInputError("Invalid JSON archive") from exc
    if size > MAX_BYTES:
        raise PayloadTooLargeError("Portable session exceeds 1 MiB")


def validate_archive(payload: Any) -> dict:
    """Validate all data before any catalog or disk mutation."""
    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        _invalid("unsupported archive fields")
    if payload["format"] != FORMAT or type(payload["version"]) is not int or payload["version"] != VERSION:
        _invalid("unsupported format or version")
    title = payload["title"]
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
        _invalid("title is missing or too long")
    messages = payload["messages"]
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        _invalid("invalid message count")
    seen: set[str] = set()
    clean: list[dict] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict) or set(item) != _MESSAGE_KEYS:
            _invalid("message fields must be id, parent_id, role and content")
        mid, parent, role, content = (
            item["id"], item["parent_id"], item["role"], item["content"]
        )
        if not isinstance(mid, str) or not _ID.fullmatch(mid) or mid in seen:
            _invalid(f"invalid or duplicate message ID at index {index}")
        # Requiring an earlier parent makes the serialized graph acyclic and
        # ensures import order agrees with the actual HistoryManager tree.
        if parent is not None and (
            not isinstance(parent, str) or not _ID.fullmatch(parent) or parent not in seen
        ):
            _invalid(f"unknown or forward parent at index {index}")
        if not isinstance(role, str) or role not in _ROLES:
            _invalid(f"unsupported message role at index {index}")
        if not isinstance(content, str) or len(content) > MAX_CONTENT_CHARS:
            _invalid(f"message content exceeds limit at index {index}")
        seen.add(mid)
        clean.append({"id": mid, "parent_id": parent, "role": role, "content": content})

    cursor = payload["cursor_id"]
    if (cursor is None and messages) or (cursor is not None and (
        not isinstance(cursor, str) or cursor not in seen
    )):
        _invalid("cursor must select a visible message")
    labels = payload["labels"]
    if not isinstance(labels, dict) or len(labels) > MAX_MESSAGES:
        _invalid("invalid labels")
    cleaned_labels: dict[str, str] = {}
    for key, value in labels.items():
        if not isinstance(key, str) or key not in seen:
            _invalid("label references an unknown message")
        if not isinstance(value, str) or len(value) > MAX_LABEL_CHARS:
            _invalid("label too long")
        cleaned_labels[key] = value

    result = {
        "format": FORMAT, "version": VERSION, "title": title.strip(),
        "cursor_id": cursor, "messages": clean, "labels": cleaned_labels,
    }
    _within_size(result)
    return result


def _fresh_id(used: set[str]) -> str:
    while True:
        value = secrets.token_hex(4)
        if value not in used:
            used.add(value)
            return value


def export_snapshot(snapshot: dict, title: str) -> dict:
    """Copy out only user-facing messages and bridge hidden parent nodes.

    Internal non-visible nodes (tool results, summaries, compaction / labels)
    may occur between two visible messages. Follow their parent link to the
    nearest prior visible ancestor. Never include hidden node content.
    """
    if not isinstance(snapshot, dict):
        _invalid("session snapshot is invalid")
    original = snapshot.get("history_entries")
    tree_mode = isinstance(original, list) and bool(original)
    if not tree_mode:
        original = snapshot.get("history_messages") or []
    if not isinstance(original, list):
        _invalid("session history is invalid")

    raw_nodes: dict[str, dict] = {}
    used: set[str] = set()
    normalized: list[tuple[str, dict]] = []
    previous: str | None = None
    for index, entry in enumerate(original):
        if not isinstance(entry, dict):
            _invalid("malformed history entry")
        if tree_mode:
            eid = entry.get("id")
            if not isinstance(eid, str) or not _ID.fullmatch(eid) or eid in used:
                _invalid("malformed or duplicate history node ID")
            used.add(eid)
            parent = entry.get("parentId")
        else:
            eid = entry.get("message_id")
            if not isinstance(eid, str) or not _ID.fullmatch(eid) or eid in used:
                eid = _fresh_id(used)
            else:
                used.add(eid)
            parent = entry.get("parent_id", previous)
            if parent is None and previous is not None:
                parent = previous
        node = {
            "id": eid, "parent_id": parent,
            "role": entry.get("role"), "content": entry.get("content", ""),
            "type": entry.get("type", "message"),
        }
        raw_nodes[eid] = node
        normalized.append((eid, node))
        previous = eid

    def closest_visible(start: str | None, visible: set[str]) -> str | None:
        cur, visited = start, set()
        while cur is not None:
            if not isinstance(cur, str) or cur in visited:
                _invalid("invalid or cyclic snapshot parent")
            if cur in visible:
                return cur
            visited.add(cur)
            node = raw_nodes.get(cur)
            if node is None:
                _invalid("missing snapshot ancestor")
            cur = node["parent_id"]
        return None

    visible_ids = {
        eid for eid, node in normalized
        if node["type"] == "message" and isinstance(node["role"], str)
        and node["role"] in _ROLES
    }
    visible: list[dict] = []
    emitted: set[str] = set()
    for eid, node in normalized:
        if eid not in visible_ids:
            continue
        content = node["content"]
        if not isinstance(content, str) or len(content) > MAX_CONTENT_CHARS:
            _invalid("message too long to export")
        parent = closest_visible(node["parent_id"], visible_ids)
        if parent is not None and parent not in emitted:
            _invalid("non-chronological history tree")
        visible.append({
            "id": eid, "parent_id": parent,
            "role": node["role"], "content": content,
        })
        emitted.add(eid)
    if len(visible) > MAX_MESSAGES:
        raise PayloadTooLargeError("Too many messages to export")

    cursor = snapshot.get("cursor_id")
    if cursor is None:
        cursor = previous
    cursor = closest_visible(cursor, visible_ids)
    raw_labels = snapshot.get("labels") or {}
    if not isinstance(raw_labels, dict):
        _invalid("malformed labels")
    labels = {
        k: v for k, v in raw_labels.items()
        if k in visible_ids and isinstance(v, str) and len(v) <= MAX_LABEL_CHARS
    }
    return validate_archive({
        "format": FORMAT, "version": VERSION,
        "title": (title or "Imported session")[:MAX_TITLE_CHARS],
        "cursor_id": cursor, "messages": visible, "labels": labels,
    })


def to_internal_snapshot(archive: dict) -> dict:
    """Generate clean v2 state; intentionally do not revive runtime state."""
    from core.session_store import build_session_snapshot

    archive = validate_archive(archive)
    messages = archive["messages"]
    entries = [
        {
            "type": "message", "id": m["id"], "parentId": m["parent_id"],
            "role": m["role"], "content": m["content"],
        }
        for m in messages
    ]
    history = [
        {
            "role": m["role"], "content": m["content"],
            "message_id": m["id"], "parent_id": m["parent_id"], "metadata": {},
        }
        for m in messages
    ]
    return build_session_snapshot(
        system_messages=[], history_messages=history, tool_schema=[],
        project_root=".", schema_version=2, history_entries=entries,
        cursor_id=archive["cursor_id"], labels=archive["labels"],
        teams_snapshot={}, read_cache={}, current_model=None,
    )
