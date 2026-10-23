"""Session store maintenance: audit, repair and migrate.

Wraps the session-consistency primitives in something an operator can run:

    python -m ridgecode maintain --dry-run
    python -m ridgecode maintain
    python -m ridgecode maintain --json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Optional

from core.session_manager import SessionManager

DEFAULT_SESSIONS_DIR = "memory/sessions"


def run(
    sessions_dir: Optional[str] = None,
    *,
    dry_run: bool = False,
    output: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Audit a session store, then (unless dry-run) repair and migrate it."""
    directory = Path(sessions_dir or DEFAULT_SESSIONS_DIR)
    emit = output or (lambda _line: None)

    if not directory.exists():
        result = {
            "sessions_dir": str(directory),
            "exists": False,
            "sessions": 0,
            "states": {},
            "repair": {},
            "migration": {},
            "dry_run": dry_run,
        }
        emit(f"Session store not found: {directory}")
        return result

    manager = SessionManager(sessions_dir=str(directory))
    states: dict[str, int] = {}
    for session_id in manager.session_ids():
        state = manager.audit_session(session_id)["state"]
        states[state] = states.get(state, 0) + 1

    result: dict[str, Any] = {
        "sessions_dir": str(directory),
        "exists": True,
        "sessions": sum(states.values()),
        "states": states,
        "repair": {},
        "migration": {},
        "dry_run": dry_run,
    }
    if not dry_run:
        result["repair"] = manager.reconcile_sessions(repair=True)
        result["migration"] = manager.migrate_sessions()

    emit(f"Session store: {directory}")
    emit(f"  sessions: {result['sessions']}")
    if states:
        detail = ", ".join(f"{name}={count}" for name, count in sorted(states.items()))
        emit(f"  states: {detail}")
    if dry_run:
        emit("  dry run: no files changed")
    else:
        repair = result["repair"]
        detail = ", ".join(f"{name}={count}" for name, count in sorted(repair.items()))
        migrated = int(result["migration"].get("migrated", 0))
        emit(f"  repair: {detail or 'none'}  migrated: {migrated}")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ridgecode maintain", description="Audit and repair the local session store",
    )
    parser.add_argument("--sessions-dir", default=DEFAULT_SESSIONS_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Report only; change nothing")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    lines: list[str] = []
    result = run(args.sessions_dir, dry_run=args.dry_run, output=lines.append)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
