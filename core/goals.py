"""Session-scoped goal state.

A goal turns a single request into an objective that can outlive one round: it
records the objective, a round budget and a terminal state. This module owns
only the durable state machine (modelled on DeepSeek Harness' ``dsh-goal``);
the round driver that decides *when* to continue lives elsewhere.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

ACTIVE = "active"
COMPLETE = "complete"
BLOCKED = "blocked"
DEFAULT_MAX_ROUNDS = 8
MAX_OBJECTIVE_CHARS = 4000

_SESSION_ID = re.compile(r"[0-9a-f]{12}\Z")


@dataclass
class Goal:
    session_id: str
    objective: str
    status: str = ACTIVE
    rounds_started: int = 0
    max_rounds: int = DEFAULT_MAX_ROUNDS
    blocked_reason: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GoalError(Exception):
    """A rejected state transition."""


class GoalStore:
    """One goal per session, persisted atomically under ``goals/``."""

    def __init__(self, directory: str | Path):
        self._dir = Path(directory)

    def _path(self, session_id: str) -> Path:
        if not _SESSION_ID.fullmatch(session_id or ""):
            raise GoalError("Invalid session id")
        return self._dir / f"{session_id}.json"

    def get(self, session_id: str) -> Optional[Goal]:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return Goal(
            session_id=session_id,
            objective=str(raw.get("objective", "")),
            status=str(raw.get("status", ACTIVE)),
            rounds_started=int(raw.get("rounds_started", 0) or 0),
            max_rounds=int(raw.get("max_rounds", DEFAULT_MAX_ROUNDS) or DEFAULT_MAX_ROUNDS),
            blocked_reason=str(raw.get("blocked_reason", "")),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )

    def set_objective(
        self, session_id: str, objective: str, *, max_rounds: int = DEFAULT_MAX_ROUNDS,
    ) -> Goal:
        text = " ".join(str(objective or "").split())
        if not text:
            raise GoalError("objective is required")
        if len(text) > MAX_OBJECTIVE_CHARS:
            text = text[:MAX_OBJECTIVE_CHARS].rstrip()
        now = _now()
        existing = self.get(session_id)
        goal = Goal(
            session_id=session_id,
            objective=text,
            status=ACTIVE,
            rounds_started=0,
            max_rounds=max(1, min(50, int(max_rounds))),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self._save(goal)
        return goal

    def start_round(self, session_id: str) -> Goal:
        goal = self._require(session_id)
        if goal.status != ACTIVE:
            raise GoalError(f"Goal is {goal.status}; no round to start")
        if goal.rounds_started >= goal.max_rounds:
            raise GoalError(f"Round budget exhausted ({goal.rounds_started}/{goal.max_rounds})")
        goal.rounds_started += 1
        goal.updated_at = _now()
        self._save(goal)
        return goal

    def complete(self, session_id: str) -> Goal:
        goal = self._require(session_id)
        if goal.status == COMPLETE:
            return goal
        goal.status = COMPLETE
        goal.updated_at = _now()
        self._save(goal)
        return goal

    def block(self, session_id: str, reason: str) -> Goal:
        text = " ".join(str(reason or "").split())
        if not text:
            raise GoalError("blocked reasons must be concrete")
        goal = self._require(session_id)
        goal.status = BLOCKED
        goal.blocked_reason = text[:1000]
        goal.updated_at = _now()
        self._save(goal)
        return goal

    def clear(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink(missing_ok=True)
        return True

    # ------------------------------------------------------------------
    def _require(self, session_id: str) -> Goal:
        goal = self.get(session_id)
        if goal is None:
            raise GoalError("No goal is set for this session")
        return goal

    def _save(self, goal: Goal) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(goal.session_id)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(goal.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
