"""Opt-in container command backend. Not a host-path "soft sandbox".

This backend is *only* for command execution through an ephemeral Docker
container. The caller must separately prevent all host-side tool bypasses.
Docker image is preloaded; no daemon-triggered pull or credential propagation.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class SandboxUnavailable(RuntimeError):
    """Fail closed: never fall back to running a command on the host."""


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool = False


class DockerCommandProvider:
    def __init__(
        self,
        workspace: str | Path,
        *,
        image: str = "python:3.12-alpine",
        memory: str = "512m",
        cpus: float = 1.0,
        pids_limit: int = 64,
        docker_binary: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ):
        if os.name != "posix":
            raise SandboxUnavailable("Docker sandbox currently requires a POSIX host")
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise SandboxUnavailable("Sandbox workspace is not a directory")
        # Docker --mount uses comma-separated key/value syntax. Reject hosts
        # whose path would confuse the daemon mount parser.
        if any(c in str(self.workspace) for c in (",", "\n", "\r", "\x00")):
            raise SandboxUnavailable("Workspace path is incompatible with Docker mounts")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@+-]{0,199}", image) or ".." in image:
            raise SandboxUnavailable("Invalid configured Docker image name")
        if not re.fullmatch(r"[1-9][0-9]*(?:[kKmMgG])?", memory):
            raise SandboxUnavailable("Invalid Docker memory limit")
        if cpus <= 0 or cpus > 8 or pids_limit < 1 or pids_limit > 512:
            raise SandboxUnavailable("Docker resource limits out of range")
        binary = docker_binary or shutil.which("docker")
        if not binary:
            raise SandboxUnavailable("Docker is unavailable; refusing host execution")
        self.docker_binary = binary
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self._run = runner or subprocess.run

    def command_argv(self, command: str, directory: str = ".") -> tuple[list[str], str]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Command must be a non-empty string")
        if "\x00" in command:
            raise ValueError("Null byte in shell command")
        if not isinstance(directory, str):
            raise ValueError("Directory must be a path string")

        source = Path(directory)
        target = (self.workspace / source).resolve(strict=True) if not source.is_absolute() else source.resolve(strict=True)
        try:
            relative = target.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionError("Working directory leaves sandbox workspace") from exc
        if not target.is_dir():
            raise ValueError("Working directory is not a directory")

        name = f"wodeagent-{secrets.token_hex(10)}"
        # Never inherit root identity into the container, even if the host
        # launches the service as root. Non-root projects should use the
        # workspace owner's UID/GID; root hosts fall back to nobody.
        uid = os.getuid() or 65534
        gid = os.getgid() or 65534
        # The Docker daemon executes the command *inside* the container, not
        # in the caller's host shell. It has no network, capabilities, root
        # identity, inherited env, Docker socket or writable root filesystem.
        argv = [
            self.docker_binary, "run",
            "--rm", "--pull=never", "--name", name,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit", str(self.pids_limit),
            "--memory", self.memory, "--cpus", str(self.cpus),
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--mount", f"type=bind,source={self.workspace},target=/workspace",
            "--workdir", "/workspace" + ("/" + relative.as_posix() if relative != Path(".") else ""),
            "--user", f"{uid}:{gid}",
            "--env", "HOME=/tmp",
            "--entrypoint", "/bin/sh",
            self.image, "-lc", command,
        ]
        return argv, name

    def execute(self, command: str, directory: str = ".", timeout_seconds: float = 120) -> CommandResult:
        if timeout_seconds <= 0 or timeout_seconds > 600:
            raise ValueError("Command timeout is out of range")
        argv, container_name = self.command_argv(command, directory)
        try:
            result = self._run(
                argv, capture_output=True, text=True, timeout=timeout_seconds,
                check=False,
            )
            return CommandResult(
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired as exc:
            # Killing only the Docker client is insufficient: remove the
            # daemon-managed container too, with an independent short limit.
            try:
                self._run(
                    [self.docker_binary, "rm", "--force", container_name],
                    capture_output=True, text=True, timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return CommandResult(stdout, stderr, None, timed_out=True)
        except OSError as exc:
            raise SandboxUnavailable("Docker invocation failed; host fallback is forbidden") from exc
