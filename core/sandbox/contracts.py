"""Shared command-only sandbox contract.

Ridge Code currently exposes only Bash to isolated backends. Provider APIs for
files, uploads and scoped warm environments will be added independently; do not
confuse this narrow contract with DeerFlow's complete Sandbox interface.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool = False
    interrupted: bool = False


class SandboxUnavailable(RuntimeError):
    """An isolated backend cannot be used; never fall back to host execution."""


class SandboxCommandProvider(Protocol):
    workspace: Path
    kind: str

    def execute(
        self, command: str, directory: str = ".",
        timeout_seconds: float = 120, *, cancel_event: Event | None = None,
    ) -> CommandResult: ...
