"""Trusted adapter for leased, one-shot Docker-hosted command turns.

The existing HostedTurnWorker enforces OIDC scope, PostgreSQL fences and Redis
admission. This runner supplies an actual isolated execution backend instead
of relying on a duck-typed fake. Every turn gets an EMPTY ephemeral workspace:
no tenant host tree, credentials, arbitrary mount, network or Docker socket.
This is a limited command execution slice, NOT full hosted LLM Agent turns.
"""
from __future__ import annotations

import math
import tempfile
from threading import Event
from typing import Any, Callable

from core.persistence.postgres_turn_queue import LeasedTurn
from core.runtime.hosted_worker import HostedLeaseLost, HostedWorkerUnavailable
from core.runtime.tool_authorization import HostedToolAuthority
from core.sandbox.docker_provider import DockerCommandProvider


class DockerHostedTurnRunner:
    isolated_sandbox = True

    def __init__(
        self, *, image: str = "python:3.12-alpine",
        memory: str = "512m", cpus: float = 1,
        pids_limit: int = 64,
        provider_factory: Callable[..., Any] = DockerCommandProvider,
    ):
        if not callable(provider_factory):
            raise TypeError("Trusted Docker provider factory required")
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self._provider_factory = provider_factory

    def __call__(
        self, turn: LeasedTurn, authority: HostedToolAuthority,
        cancelled: Event,
    ) -> dict[str, Any]:
        if type(turn) is not LeasedTurn or type(authority) is not HostedToolAuthority:
            raise HostedWorkerUnavailable("Verified lease and identity required")
        if not isinstance(cancelled, Event):
            raise TypeError("Cancellation event required")
        if cancelled.is_set() or not authority.allows("Bash"):
            raise HostedLeaseLost("Membership revoked before sandbox execution")
        payload = turn.payload
        if not isinstance(payload, dict) or set(payload) != {"kind", "command", "timeout_seconds"}:
            raise ValueError("Hosted command job requires exactly kind, command and timeout_seconds")
        command = payload["command"]
        timeout = payload["timeout_seconds"]
        if payload["kind"] != "docker-command" or not isinstance(command, str) or not 0 < len(command) <= 4096:
            raise ValueError("Unsupported or oversized hosted command")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("Hosted command timeout must be 0..120 seconds")

        # Never bind the source project directory, user-supplied file paths,
        # configured credentials or a shared warm guest to hosted jobs.
        with tempfile.TemporaryDirectory(prefix="ridge-hosted-job-") as workspace:
            provider = self._provider_factory(
                workspace, image=self.image, memory=self.memory,
                cpus=self.cpus, pids_limit=self.pids_limit,
            )
            if type(provider) is DockerCommandProvider and provider.workspace.resolve() != __import__("pathlib").Path(workspace).resolve():
                raise HostedWorkerUnavailable("Hosted sandbox unexpectedly changed workspace")
            try:
                result = provider.execute(
                    command, timeout_seconds=float(timeout),
                    cancel_event=cancelled,
                )
            finally:
                close = getattr(provider, "close", None)
                if callable(close):
                    close()
        if cancelled.is_set() or result.interrupted or not authority.allows("Bash"):
            raise HostedLeaseLost("Hosted execution lost membership or cancellation fence")
        # Never publish enormous opaque command output. A guest failure is an
        # ordinary fenced terminal tool result, not proof of model success.
        stdout = result.stdout[:32768]
        stderr = result.stderr[:32768]
        return {
            "kind": "docker-command",
            "exit_code": result.exit_code,
            "timed_out": bool(result.timed_out),
            "truncated": bool(result.truncated
                              or len(result.stdout) > 32768 or len(result.stderr) > 32768),
            "stdout": stdout,
            "stderr": stderr,
        }
