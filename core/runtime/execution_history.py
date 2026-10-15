"""Durable, bounded per-session tool execution projection.

This is intentionally *not* the raw event journal or an audit of prompts.
Only tool names, status and bounded redacted summaries are persisted. Runs and
agent event streams remain owned by the runtime. SQLite supplies atomic writes
across threads and application restarts without keeping JSONL credentials.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from core.context_engine.trace_sanitizer import TraceSanitizer
from core.events import AgentEvent

log = logging.getLogger(__name__)
SESSION = re.compile(r"[0-9a-f]{12}\Z")
MAX_ITEMS = 200
MAX_TEXT = 2048


class ExecutionHistory:
    def __init__(self, workspace_base: str | Path):
        self.db_path = Path(workspace_base).parent / "execution-history.sqlite3"
        self._sanitizer = TraceSanitizer(enable=True)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.db_path.is_symlink():
            raise OSError("Execution database may not be a symlink")
        with closing(sqlite3.connect(self.db_path, timeout=5)) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tools (
                sid TEXT NOT NULL, call_id TEXT NOT NULL,
                step INTEGER NOT NULL, name TEXT NOT NULL,
                status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (sid, call_id)
            )""")
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            log.warning("Cannot set private execution database permissions")
        return sqlite3.connect(self.db_path, timeout=5)

    def append(self, sid: str, event: AgentEvent) -> None:
        if not SESSION.fullmatch(sid) or event.type not in {"tool.started", "tool.completed"}:
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        call_id = str(payload.get("tool_call_id") or "")[:128]
        if not call_id:
            return
        name = str(payload.get("tool") or payload.get("name") or "Tool")[:100]
        status = "running" if event.type == "tool.started" else (
            "success" if payload.get("status") == "success" else "error"
        )
        summary = ""
        if event.type == "tool.completed":
            # Only structured container outputs; never store arbitrary model,
            # command-line, environment or MCP raw tool responses.
            try:
                output = json.loads(str(payload.get("output") or ""))
                data = output.get("data") if isinstance(output, dict) else None
                if isinstance(data, dict) and data.get("sandbox") in {"docker", "boxlite"}:
                    summary = json.dumps(self._sanitizer.sanitize({
                        "sandbox": data.get("sandbox"),
                        "exit_code": data.get("exit_code") if type(data.get("exit_code")) is int else None,
                        "stdout": str(data.get("stdout") or "")[:MAX_TEXT],
                        "stderr": str(data.get("stderr") or "")[:MAX_TEXT],
                    }), ensure_ascii=False)
            except (ValueError, TypeError, UnicodeError):
                pass
        with closing(self._connect()) as conn, conn:
            conn.execute("""INSERT INTO tools (sid,call_id,step,name,status,summary)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(sid,call_id) DO UPDATE SET
                    status=excluded.status,
                    name=CASE WHEN excluded.name='Tool' THEN tools.name ELSE excluded.name END,
                    summary=excluded.summary""",
                (sid, call_id, max(0, int(event.step or 0)), name, status, summary))
            conn.execute("""DELETE FROM tools
                WHERE sid=? AND call_id NOT IN
                  (SELECT call_id FROM tools WHERE sid=? ORDER BY rowid DESC LIMIT ?)""",
                (sid, sid, MAX_ITEMS))

    def list(self, sid: str) -> dict:
        if not SESSION.fullmatch(sid):
            raise ValueError("Invalid session ID")
        if not self.db_path.exists():
            return {"tools": []}
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT call_id,step,name,status,summary FROM tools WHERE sid=? ORDER BY rowid ASC LIMIT ?",
                (sid, MAX_ITEMS),
            ).fetchall()
        tools = []
        for call_id, step, name, status, summary in rows:
            tools.append({
                "id": call_id, "step": step, "name": name,
                "status": status, "input": {},
                "output": json.dumps({"data": json.loads(summary)}) if summary else None,
            })
        return {"tools": tools}

    def delete(self, sid: str) -> None:
        if not SESSION.fullmatch(sid) or not self.db_path.exists():
            return
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM tools WHERE sid=?", (sid,))
