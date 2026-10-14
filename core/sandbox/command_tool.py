"""Bash-compatible tool routed exclusively to the container provider."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.sandbox.docker_provider import DockerCommandProvider, SandboxUnavailable
from core.runtime.cancellation import current_cancel_event
from tools.builtin.bash import BashTool
from tools.base import ErrorCode


class SandboxBashTool(BashTool):
    usage_notes = (
        "Bash: runs exclusively in an ephemeral isolated Docker container, "
        "working directory /workspace. The container has no outbound network "
        "and no host credentials. Read/write files using shell commands inside "
        "/workspace; host Read/Write/MCP tools are unavailable in Docker mode."
    )

    def __init__(self, provider: DockerCommandProvider):
        super().__init__(name="Bash", project_root=provider.workspace)
        self.provider = provider
        self.description = (
            "Run a shell command within an isolated, network-disabled Docker "
            "container. Workspace is mounted at /workspace. Only the workspace "
            "volume persists between commands."
        )

    def run(self, parameters: dict[str, Any]) -> str:
        start = time.monotonic()
        command = parameters.get("command")
        directory = parameters.get("directory", ".")
        timeout_ms = parameters.get("timeout_ms", self.DEFAULT_TIMEOUT_MS)
        if not isinstance(command, str) or not command.strip():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Command must be a non-empty string.",
                params_input=parameters,
            )
        if not isinstance(directory, str):
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Directory must be a string.",
                params_input=parameters,
            )
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= self.MAX_TIMEOUT_MS:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="timeout_ms is outside its allowed range.",
                params_input=parameters,
            )
        try:
            result = self.provider.execute(
                command, directory=directory, timeout_seconds=timeout_ms / 1000,
                cancel_event=current_cancel_event(),
            )
        except (PermissionError, ValueError, FileNotFoundError) as exc:
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message="Sandbox denied the requested working directory or command.",
                params_input=parameters,
            )
        except SandboxUnavailable:
            return self.create_error_response(
                error_code=ErrorCode.EXECUTION_ERROR,
                message="Docker sandbox is unavailable; host execution is forbidden.",
                params_input=parameters,
            )
        elapsed = int((time.monotonic() - start) * 1000)
        # Keep the universal response protocol and bounded output. Retained
        # stdout/stderr can contain untrusted data; don't echo host exceptions.
        stdout = result.stdout[: self.OUTPUT_MAX_BYTES]
        stderr = result.stderr[: self.OUTPUT_MAX_BYTES]
        truncated = len(result.stdout) > len(stdout) or len(result.stderr) > len(stderr)
        data = {
            "stdout": stdout, "stderr": stderr,
            "exit_code": result.exit_code,
            "truncated": truncated, "command": command,
            "directory": directory, "sandbox": "docker",
        }
        if result.interrupted:
            return self.create_error_response(
                error_code=ErrorCode.INTERRUPTED,
                message="Docker command interrupted; container cleanup requested.",
                params_input=parameters, time_ms=elapsed,
            )
        if result.timed_out:
            return self.create_error_response(
                error_code=ErrorCode.TIMEOUT,
                message="Docker command timed out; container cleanup requested.",
                params_input=parameters, time_ms=elapsed,
            )
        if result.exit_code == 0 and not truncated:
            return self.create_success_response(
                data=data, text=stdout or "Container command succeeded",
                params_input=parameters, time_ms=elapsed,
            )
        return self.create_partial_response(
            data=data, text=(stdout or stderr or "Container command failed")[:2000],
            params_input=parameters, time_ms=elapsed,
        )
