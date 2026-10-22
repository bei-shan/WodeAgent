"""Multi-session manager — create, list, switch, and persist conversation sessions.

Each session is stored as a JSON snapshot under ``memory/sessions/``,
with an ``index.json`` tracking all sessions.

Usage::

    mgr = SessionManager(sessions_dir="memory/sessions")
    session_id = mgr.create_session()
    mgr.save_session(session_id, snapshot_dict)
    mgr.list_sessions()  # [{id, title, created_at, ...}]
    snapshot = mgr.load_session(session_id)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.session_log import LOG_SUFFIX, SessionLog
from core.session_migrations import migrate_snapshot, needs_migration


# Local single-process safety only. A multi-instance deployment needs a
# transactional shared session store (e.g. PostgreSQL), not JSON indexes.
_INDEX_WRITE_LOCK = threading.RLock()

# Snapshots are ``<12 hex>.json``; turn sidecars (``<id>.turn.json``) and the
# index itself must never be treated as sessions during reconciliation.
_SESSION_FILE = re.compile(r"([0-9a-f]{12})\.json\Z")
_SESSION_ID = re.compile(r"[0-9a-f]{12}\Z")


@dataclass
class SessionInfo:
    """Lightweight session metadata stored in index.json."""

    id: str
    title: str
    created_at: str
    modified_at: str
    message_count: int = 0
    preview: str = ""


class SessionManager:
    """Manages multiple conversation sessions with index + snapshot files.

    Parameters
    ----------
    sessions_dir:
        Directory for session storage (default ``memory/sessions``).
    auto_title_max_len:
        Max length for auto-generated titles from first user message.
    """

    INDEX_FILE = "index.json"

    def __init__(
        self,
        sessions_dir: str = "memory/sessions",
        auto_title_max_len: int = 60,
    ):
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._auto_title_max_len = auto_title_max_len
        # index.json is a cache over snapshots: reconcile lazily, once per
        # manager instance, so a crashed or manual edit cannot leave ghosts.
        self._reconciled = False
        self._reconcile_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_session(self, title: str = "") -> str:
        """Create a new session and return its ID.

        If *title* is empty, a placeholder title is generated.
        """
        session_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        info = SessionInfo(
            id=session_id,
            title=title or f"New session {now[:16]}",
            created_at=now,
            modified_at=now,
        )
        self._append_index(info)
        # Write an empty snapshot so the file exists (current schema).
        self._write_snapshot(session_id, {
            "version": 2,
            "schema_version": 2,
            "history_messages": [],
        })
        # Every session gets its log at birth so snapshot and log never start
        # out of sync.
        self._sync_session_log(session_id, {
            "version": 2,
            "schema_version": 2,
            "history_messages": [],
            "created_at": now,
        })
        return session_id

    def save_session(self, session_id: str, snapshot: dict[str, Any]) -> None:
        """Persist a session snapshot and update the index.

        The durable log is written first: a crash between the two writes leaves
        the log ahead of the snapshot, and readers prefer the log. The snapshot
        stays the compatibility export. Automatically extracts *title* from the
        first user message if the current title is a placeholder.
        """
        self._sync_session_log(session_id, snapshot)
        self._write_snapshot(session_id, snapshot)
        history = snapshot.get("history_messages") or []
        message_count = len(history)
        preview = ""
        title = ""

        # Extract title from first user message.
        for msg in history:
            if msg.get("role") == "user":
                content = str(msg.get("content", "")).strip()
                if content:
                    preview = content[:100]
                    title = content[: self._auto_title_max_len]
                    break

        self._update_index(
            session_id,
            title=title,
            message_count=message_count,
            preview=preview,
            only_overwrite_placeholder=True,
        )

    def load_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """Load a session snapshot by ID.  Returns ``None`` if not found.

        A lost snapshot is rebuilt from the append-only log when one exists.
        """
        path = self._snapshot_path(session_id)
        if not path.exists():
            log = SessionLog(self._dir, session_id)
            # A header-only log is a session that was deleted, not one to revive.
            return log.to_snapshot() if log.exists() and log.has_events() else None
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(snapshot, dict):
            return None
        snapshot = self._prefer_log_history(session_id, snapshot)
        self._repair_log_behind_snapshot(session_id, snapshot)
        if needs_migration(snapshot):
            snapshot = self._migrate_on_load(session_id, snapshot)
        return snapshot

    def _prefer_log_history(self, session_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Use the log's history when it is strictly ahead of the snapshot.

        The log is written before the snapshot, so a crash can leave the log
        ahead. A shorter log (older code, manual edit) is left to the repair
        pass instead. Branch/rollback sessions are republished, so the log never
        carries a stale longer history for them.
        """
        log = SessionLog(self._dir, session_id)
        if not log.exists():
            return snapshot
        try:
            logged = log.derive_messages()
        except OSError:
            return snapshot
        current = snapshot.get("history_messages") or []
        if len(logged) <= len(current):
            return snapshot
        merged = dict(snapshot)
        merged["history_messages"] = logged
        # Never replace an existing history tree with a linear projection: the
        # snapshot may carry forks the log does not describe.
        entries = log.derive_entries()
        if entries and not snapshot.get("history_entries"):
            merged["history_entries"] = entries
        cursor = log.read_cursor()
        if cursor:
            merged["cursor_id"] = cursor
        return merged

    def _migrate_on_load(self, session_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Migrate a legacy snapshot once, then persist the stamped version."""
        try:
            migrated = migrate_snapshot(snapshot, log=SessionLog(self._dir, session_id))
            if migrated != snapshot:
                self._write_snapshot(session_id, migrated)
            return migrated
        except (OSError, TypeError, ValueError):
            return snapshot

    def migrate_sessions(self) -> dict[str, int]:
        """Stamp every legacy snapshot with the current schema version."""
        migrated = 0
        for path in self._dir.glob("*.json"):
            match = _SESSION_FILE.fullmatch(path.name)
            if not match:
                continue
            try:
                snapshot = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(snapshot, dict) or not needs_migration(snapshot):
                continue
            self.load_session(match.group(1))
            migrated += 1
        return {"migrated": migrated}

    # ------------------------------------------------------------------
    # Consistency audit (snapshot vs append-only log)
    # ------------------------------------------------------------------
    def audit_session(self, session_id: str) -> dict[str, Any]:
        """Compare one session's snapshot and log without changing either.

        States: ``snapshot_only``, ``log_only``, ``log_ahead`` (crash before the
        snapshot write), ``log_behind`` (older writer), ``diverged`` (same
        length, different content) and ``ok``.
        """
        snapshot = self._read_snapshot_raw(session_id)
        log = SessionLog(self._dir, session_id)
        report: dict[str, Any] = {
            "session_id": session_id,
            "snapshot_messages": len(snapshot.get("history_messages") or []) if snapshot else None,
            "log_events": None,
            "tree_entries": len(snapshot.get("history_entries") or []) if snapshot else None,
            "state": "ok",
        }
        if snapshot is None:
            has_log = log.exists() and log.has_events()
            report["state"] = "log_only" if has_log else "missing"
            if has_log:
                report["log_events"] = len(log.read_events())
            return report
        if not log.exists():
            report["state"] = "snapshot_only"
            return report

        logged = log.derive_messages()
        report["log_events"] = len(logged)
        current = snapshot.get("history_messages") or []
        if len(logged) > len(current):
            report["state"] = "log_ahead"
        elif len(logged) < len(current):
            report["state"] = "log_behind"
        elif not _same_messages(logged, current):
            report["state"] = "diverged"
        return report

    def reconcile_sessions(self, *, repair: bool = True) -> dict[str, int]:
        """Audit every session and repair logs that lag or diverge.

        ``log_ahead`` is left alone: the read path already prefers the log.
        Repairs are one-directional (log follows snapshot) so a crash never
        silently drops events the snapshot still holds.
        """
        counts: dict[str, int] = {}
        session_ids: set[str] = set()
        for path in self._dir.glob("*.json"):
            match = _SESSION_FILE.fullmatch(path.name)
            if match:
                session_ids.add(match.group(1))
        for path in self._dir.glob(f"*{LOG_SUFFIX}"):
            session_id = path.name[: -len(LOG_SUFFIX)]
            if _SESSION_ID.fullmatch(session_id):
                session_ids.add(session_id)

        for session_id in sorted(session_ids):
            report = self.audit_session(session_id)
            state = report["state"]
            if repair and state in {"log_behind", "diverged"}:
                snapshot = self._read_snapshot_raw(session_id)
                if snapshot is not None:
                    try:
                        SessionLog(self._dir, session_id).rewrite(snapshot)
                        state = "repaired"
                    except (OSError, TypeError, ValueError):
                        state = "repair_failed"
            counts[state] = counts.get(state, 0) + 1
        return counts

    def _read_snapshot_raw(self, session_id: str) -> Optional[dict[str, Any]]:
        path = self._snapshot_path(session_id)
        if not path.exists():
            return None
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return snapshot if isinstance(snapshot, dict) else None

    def _repair_log_behind_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        """Append events the log is missing so both views agree on read."""
        log = SessionLog(self._dir, session_id)
        if not log.exists():
            return
        try:
            logged = len(log.derive_messages())
            current = len(snapshot.get("history_messages") or [])
            if logged < current:
                log.sync(snapshot)
        except (OSError, TypeError, ValueError):
            pass

    def snapshot_exists(self, session_id: str) -> bool:
        """Return ``True`` when a snapshot file is present for *session_id*.

        Cheap existence probe used to hide index entries whose snapshot was
        removed outside the catalog (manual cleanup, interrupted restore).
        """
        return self._snapshot_path(session_id).exists()

    def list_sessions(self) -> list[SessionInfo]:
        """Return all sessions sorted by *modified_at* descending."""
        self._ensure_reconciled()
        return sorted(
            self._load_index(),
            key=lambda s: s.modified_at,
            reverse=True,
        )

    def get_session(self, session_id: str) -> Optional[SessionInfo]:
        """Return metadata for a single session, or ``None``."""
        self._ensure_reconciled()
        for s in self._load_index():
            if s.id == session_id:
                return s
        return None

    def resolve_identifier(self, identifier: str) -> Optional[str]:
        """Resolve a session identifier to a session ID.

        Supports:
        - Full session ID (12 hex chars)
        - Numeric index in the session list (1-based, as shown in list)
        - Prefix match (min 4 chars)
        """
        identifier = identifier.strip()
        if not identifier:
            return None

        sessions = self.list_sessions()

        # Exact ID match.
        for s in sessions:
            if s.id == identifier:
                return s.id

        # Numeric index (1-based).
        # Canonical numeric list indexes do not contain leading zeros.
        # Otherwise a hex-ID prefix such as "01" could accidentally select
        # list item 1 instead of respecting the 4-char minimum prefix rule.
        if identifier.isdecimal() and (identifier == "0" or not identifier.startswith("0")):
            idx = int(identifier) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx].id

        # Prefix match (min 4 chars).
        if len(identifier) >= 4:
            matches = [s for s in sessions if s.id.startswith(identifier)]
            if len(matches) == 1:
                return matches[0].id

        return None

    def rename_session(self, session_id: str, title: str) -> bool:
        """Rename a session. Returns ``True`` on success."""
        with _INDEX_WRITE_LOCK:
            index = self._load_index()
            for s in index:
                if s.id == session_id:
                    s.title = title.strip()
                    self._write_index(index)
                    return True
            return False

    def delete_session(self, session_id: str) -> bool:
        """Delete a session (snapshot + index entry). Returns ``True`` on success."""
        with _INDEX_WRITE_LOCK:
            snap = self._snapshot_path(session_id)
            if snap.exists():
                try:
                    snap.unlink()
                except OSError:
                    return False

            try:
                log = SessionLog(self._dir, session_id)
                log.path.unlink(missing_ok=True)
                log.state_path.unlink(missing_ok=True)
            except OSError:
                pass

            index = self._load_index()
            new_index = [s for s in index if s.id != session_id]
            if len(new_index) == len(index):
                return False  # not found
            self._write_index(new_index)
            return True

    # ------------------------------------------------------------------
    # Index reconciliation (index.json is a cache over snapshots)
    # ------------------------------------------------------------------

    def _ensure_reconciled(self) -> None:
        """Run the reconcile pass at most once per manager instance."""
        if self._reconciled:
            return
        disabled = (os.getenv("SESSION_INDEX_RECONCILE", "true") or "").strip().lower()
        if disabled in {"0", "false", "no", "off"}:
            self._reconciled = True
            return
        with self._reconcile_lock:
            if self._reconciled:
                return
            # Set before scanning: a snapshot read must never re-enter here.
            self._reconciled = True
            try:
                self.reconcile_index()
            except Exception:
                self._reconciled = False

    def reconcile_index(self) -> dict[str, int]:
        """Make ``index.json`` agree with the durable files on disk.

        A session counts as present when either its snapshot or its append-only
        log exists. Orphans are indexed from whichever is readable; entries with
        neither are dropped. The write only happens when something changed.
        """
        with _INDEX_WRITE_LOCK:
            index = self._load_index()
            known = {item.id: item for item in index}
            snapshots: dict[str, Path] = {}
            for path in self._dir.glob("*.json"):
                match = _SESSION_FILE.fullmatch(path.name)
                if match:
                    snapshots[match.group(1)] = path
            logs: set[str] = set()
            log_suffix = LOG_SUFFIX
            for path in self._dir.glob(f"*{log_suffix}"):
                session_id = path.name[: -len(log_suffix)]
                # A header-only log does not resurrect a deleted session.
                if _SESSION_ID.fullmatch(session_id) and SessionLog(self._dir, session_id).has_events():
                    logs.add(session_id)

            available = set(snapshots) | logs
            kept = [info for session_id, info in known.items() if session_id in available]
            removed = len(known) - len(kept)
            added = 0
            for session_id in available - set(known):
                derived = self._derive_info(session_id, snapshots.get(session_id))
                if derived is None:
                    derived = self._derive_info_from_log(session_id)
                if derived is None:
                    continue
                kept.append(derived)
                added += 1

            if added or removed:
                kept.sort(key=lambda item: (item.created_at, item.id))
                self._write_index(kept)
            return {"added": added, "removed": removed}

    def _derive_info(self, session_id: str, path: Optional[Path]) -> Optional[SessionInfo]:
        """Rebuild one index entry from its snapshot, or ``None`` if unreadable."""
        if path is None:
            return None
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(snapshot, dict):
            return None
        history = snapshot.get("history_messages")
        if not isinstance(history, list):
            history = []
        title = ""
        preview = ""
        for message in history:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = str(message.get("content", "")).strip()
            if content:
                title = content[: self._auto_title_max_len]
                preview = content[:100]
                break
        stamp = self._file_stamp(path)
        return SessionInfo(
            id=session_id,
            title=title or f"Recovered session {stamp[:16]}",
            created_at=stamp,
            modified_at=stamp,
            message_count=len(history),
            preview=preview,
        )

    def _derive_info_from_log(self, session_id: str) -> Optional[SessionInfo]:
        """Rebuild one index entry from the append-only log."""
        log = SessionLog(self._dir, session_id)
        if not log.exists():
            return None
        messages = log.derive_messages()
        header = log.read_header() or {}
        stamp = str(header.get("created_at") or "") or self._file_stamp(log.path)
        title = ""
        preview = ""
        for message in messages:
            if message.get("role") != "user":
                continue
            content = str(message.get("content", "")).strip()
            if content:
                title = content[: self._auto_title_max_len]
                preview = content[:100]
                break
        return SessionInfo(
            id=session_id,
            title=title or f"Recovered session {stamp[:16]}",
            created_at=stamp,
            modified_at=stamp,
            message_count=len(messages),
            preview=preview,
        )

    @staticmethod
    def _file_stamp(path: Path) -> str:
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
        except OSError:
            return _now_iso()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _snapshot_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def _index_path(self) -> Path:
        return self._dir / self.INDEX_FILE

    def _load_index(self) -> list[SessionInfo]:
        path = self._index_path()
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        result: list[SessionInfo] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            result.append(SessionInfo(
                id=str(item.get("id", "")),
                title=str(item.get("title", "")),
                created_at=str(item.get("created_at", "")),
                modified_at=str(item.get("modified_at", "")),
                message_count=int(item.get("message_count", 0)),
                preview=str(item.get("preview", "")),
            ))
        return result

    def _write_index(self, index: list[SessionInfo]) -> None:
        data = [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "modified_at": s.modified_at,
                "message_count": s.message_count,
                "preview": s.preview,
            }
            for s in index
        ]
        # Atomic replacement prevents readers from seeing truncated JSON.
        path = self._index_path()
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _append_index(self, info: SessionInfo) -> None:
        with _INDEX_WRITE_LOCK:
            index = self._load_index()
            index.append(info)
            self._write_index(index)

    def _update_index(
        self,
        session_id: str,
        title: str = "",
        message_count: int = 0,
        preview: str = "",
        only_overwrite_placeholder: bool = False,
    ) -> None:
        with _INDEX_WRITE_LOCK:
            index = self._load_index()
            for s in index:
                if s.id == session_id:
                    s.modified_at = _now_iso()
                    if title:
                        if not only_overwrite_placeholder or s.title.startswith("New session"):
                            s.title = title
                    s.message_count = message_count
                    s.preview = preview
                    self._write_index(index)
                    return
            # The entry is missing (pruned by reconciliation or never indexed):
            # the snapshot write is authoritative, so rebuild the cache row.
            now = _now_iso()
            index.append(SessionInfo(
                id=session_id,
                title=title or f"New session {now[:16]}",
                created_at=now,
                modified_at=now,
                message_count=message_count,
                preview=preview,
            ))
            self._write_index(index)

    def _sync_session_log(self, session_id: str, snapshot: dict[str, Any]) -> None:
        """Append new events to the durable log; never fail the snapshot write."""
        try:
            SessionLog(self._dir, session_id).sync(snapshot)
        except (OSError, TypeError, ValueError):
            pass

    def _write_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        path = self._snapshot_path(session_id)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _same_messages(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    if len(left) != len(right):
        return False
    for first, second in zip(left, right):
        if (first.get("role"), first.get("content")) != (second.get("role"), second.get("content")):
            return False
    return True
