"""Canonical agent turn outcomes and safe error metadata.

All adapters (Web, CLI, tests) can consume a single terminal vocabulary.
The outcome intentionally omits exception text, tracebacks, prompts and API
request details: those may contain credentials or workspace data.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TurnStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TurnOutcome:
    status: TurnStatus
    response: str = ""
    error_kind: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "response": self.response,
            "error_kind": self.error_kind,
            "retryable": self.retryable,
        }


def failure_outcome(exc: Exception) -> TurnOutcome:
    """Classify provider failures without surfacing sensitive raw exception text."""
    from core.llm_recovery import ModelInvocationError
    if isinstance(exc, ModelInvocationError):
        return TurnOutcome(TurnStatus.FAILED, error_kind=exc.kind, retryable=exc.retryable)
    return TurnOutcome(TurnStatus.FAILED, error_kind="agent_error")


def persist_turn_outcome(session_id: str, outcome: TurnOutcome) -> None:
    """Atomically persist the most recent terminal state next to the snapshot."""
    import json
    import os
    from pathlib import Path
    from core.session_manager import SessionManager

    # Session IDs are generated hex tokens, never arbitrary file paths.
    if not session_id or any(ch not in "0123456789abcdef" for ch in session_id):
        raise ValueError("Invalid session ID")
    folder = SessionManager()._dir
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{session_id}.turn.json"
    tmp = folder / f".{session_id}.{os.getpid()}.turn.tmp"
    try:
        tmp.write_text(json.dumps(outcome.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def load_turn_outcome(session_id: str) -> TurnOutcome | None:
    """Read the last stable outcome if present, tolerating old sessions."""
    import json
    from core.session_manager import SessionManager
    if not session_id or any(ch not in "0123456789abcdef" for ch in session_id):
        return None
    target = SessionManager()._dir / f"{session_id}.turn.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return TurnOutcome(
            status=TurnStatus(data["status"]),
            response=str(data.get("response", "")),
            error_kind=data.get("error_kind"),
            retryable=bool(data.get("retryable", False)),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
