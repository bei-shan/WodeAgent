"""Opt-in container command backend. Not a host-path "soft sandbox".

This backend is *only* for command execution through an ephemeral Docker
container. The caller must separately prevent all host-side tool bypasses.
Docker image is preloaded; no daemon-triggered pull or credential propagation.
"""
from __future__ import annotations

import math
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from threading import Event
from typing import Callable

from core.runtime.process_execution import run_process
from core.sandbox.contracts import CommandResult, SandboxUnavailable
from core.sandbox.docker_lifecycle import (DockerLeaseManager, SandboxLeaseError,
    installation_key_path, shared_lease_manager)


class DockerCommandProvider:
    kind = "docker"
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
        lease_manager: DockerLeaseManager | None = None,
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
        if (isinstance(cpus, bool) or not isinstance(cpus, (int, float))
                or not math.isfinite(cpus) or not 0 < cpus <= 8
                or isinstance(pids_limit, bool) or not isinstance(pids_limit, int)
                or not 1 <= pids_limit <= 512):
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
        self._runner_injected = runner is not None
        try:
            if lease_manager is not None:
                self._leases = lease_manager
            elif runner is not None:
                # Fake runners never touch the operator's persistent key.
                self._leases = DockerLeaseManager(
                    binary, secrets.token_bytes(32), runner=runner,
                )
            else:
                secret_path = installation_key_path().resolve(strict=False)
                try:
                    secret_path.relative_to(self.workspace)
                except ValueError:
                    pass
                else:
                    raise SandboxUnavailable(
                        "Workspace would expose the private sandbox owner key"
                    )
                self._leases = shared_lease_manager(binary)
        except SandboxLeaseError as exc:
            raise SandboxUnavailable(
                "Private Docker ownership cannot be established"
            ) from exc

    def command_argv(self, command: str, directory: str = ".", *, timeout_seconds: float = 120) -> tuple[list[str], str]:
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
        labels = self._leases.labels(name, timeout_seconds)
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
            *(flag for key, value in labels.items()
              for flag in ("--label", f"{key}={value}")),
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

    def _force_remove(self, container_name: str) -> None:
        """Clean up daemon-managed containers even when the Docker CLI dies."""
        try:
            self._run(
                [self.docker_binary, "rm", "--force", container_name],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Cleanup is best effort, but host execution is never a fallback.
            pass

    def execute(
        self,
        command: str,
        directory: str = ".",
        timeout_seconds: float = 120,
        *,
        cancel_event: Event | None = None,
    ) -> CommandResult:
        """Execute with a bounded deadline and turn-scoped cooperative stop.

        The default subprocess path polls cancellation and kills the Docker CLI
        process group. The daemon container is separately force-removed on a
        timeout or interrupt; killing the CLI alone would leave work running.
        An injected runner retains the legacy synchronous testing seam.
        """
        return self._execute_impl(
            command, directory, timeout_seconds,
            cancel_event=cancel_event, stdin_data=None,
        )

    def execute_stdin(
        self, command: str, stdin_data: bytes,
        directory: str = ".", timeout_seconds: float = 120,
        *, cancel_event: Event | None = None,
    ) -> CommandResult:
        """Stream bounded bytes over Docker -i, never via command-line argv."""
        if not isinstance(stdin_data, bytes) or len(stdin_data) > 65536:
            raise ValueError("Docker stdin payload is invalid or exceeds 64 KiB")
        return self._execute_impl(
            command, directory, timeout_seconds,
            cancel_event=cancel_event, stdin_data=stdin_data,
        )

    def _execute_impl(
        self, command: str, directory: str, timeout_seconds: float,
        *, cancel_event: Event | None, stdin_data: bytes | None,
    ) -> CommandResult:
        if (isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 600):
            raise ValueError("Command timeout is out of range")
        argv, container_name = self.command_argv(
            command, directory, timeout_seconds=timeout_seconds,
        )
        if stdin_data is not None:
            # -i is the only extra Docker capability. No tty or ports.
            argv.insert(2, "-i")
        if cancel_event is not None and cancel_event.is_set():
            return CommandResult("", "", None, interrupted=True)
        self._leases.activate(container_name)
        try:
            if self._runner_injected:
                # Deterministic fake runners used by the CLI argument tests.
                result = self._run(
                    argv, capture_output=True, text=stdin_data is None,
                    **({"input": stdin_data} if stdin_data is not None else {}),
                    timeout=timeout_seconds, check=False,
                )
                if cancel_event is not None and cancel_event.is_set():
                    self._force_remove(container_name)
                    return CommandResult("", "", None, interrupted=True)
                return CommandResult(
                    stdout=(result.stdout or b"").decode("utf-8", "replace")
                    if isinstance(result.stdout, bytes) else (result.stdout or ""),
                    stderr=(result.stderr or b"").decode("utf-8", "replace")
                    if isinstance(result.stderr, bytes) else (result.stderr or ""),
                    exit_code=result.returncode,
                )

            result = run_process(
                argv, shell=False, cwd=str(self.workspace), env=os.environ.copy(),
                timeout=timeout_seconds, cancel_event=cancel_event,
                stdin_data=stdin_data,
            )
            if result.timed_out or result.interrupted:
                self._force_remove(container_name)
            return CommandResult(
                stdout=result.stdout, stderr=result.stderr,
                exit_code=result.returncode,
                timed_out=result.timed_out,
                interrupted=result.interrupted,
            )
        except subprocess.TimeoutExpired as exc:
            self._force_remove(container_name)
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return CommandResult(stdout, stderr, None, timed_out=True)
        except OSError as exc:
            raise SandboxUnavailable("Docker invocation failed; host fallback is forbidden") from exc
        finally:
            self._leases.deactivate(container_name)
