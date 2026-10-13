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
