"""Session format migrations.

Formats
-------
v1
    A JSON snapshot only: ``history_messages``, ``history_entries``,
    ``cursor_id``. Nothing durable beyond the snapshot.
v2
    The v1 snapshot plus an append-only JSONL log whose header declares
    schema 2, including ``cursor`` events for history position.

Migration is additive and idempotent: stamping the snapshot version and
publishing a log generation when the session has history but no log. Old
readers keep working on the snapshot; new readers gain the durable log.
"""
from __future__ import annotations

from typing import Any, Optional

from core.session_log import CURRENT_SCHEMA, SessionLog

LATEST = CURRENT_SCHEMA


def snapshot_version(snapshot: dict[str, Any]) -> int:
    """Return the declared schema version, treating missing values as 0."""
    try:
        return int(snapshot.get("schema_version") or 0)
    except (TypeError, ValueError):
        return 0


def needs_migration(snapshot: dict[str, Any]) -> bool:
    return snapshot_version(snapshot) < LATEST


def migrate_snapshot(
    snapshot: dict[str, Any], *, log: Optional[SessionLog] = None,
) -> dict[str, Any]:
    """Return a migrated copy of *snapshot*; never mutates the input."""
    migrated: dict[str, Any] = dict(snapshot)
    version = snapshot_version(snapshot)

    if version < 1:
        # Pre-schema snapshots: normalise the fields later versions rely on.
        migrated.setdefault("history_messages", [])
        migrated.setdefault("history_entries", [])
        migrated.setdefault("system_messages", [])
        migrated.setdefault("cursor_id", None)
        try:
            current_version = int(migrated.get("version") or 1)
        except (TypeError, ValueError):
            current_version = 1
        migrated["version"] = max(current_version, 1)

    if version < 2:
        if log is not None:
            header = log.read_header() if log.exists() else None
            try:
                header_schema = int((header or {}).get("schema") or 0)
            except (TypeError, ValueError):
                header_schema = 0
            if header is None or header_schema < 2:
                # No log yet, or a log written by an older schema.
                log.rewrite(migrated)
            else:
                log.sync(migrated)
        migrated["schema_version"] = 2

    return migrated
