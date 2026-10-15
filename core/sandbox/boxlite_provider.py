"""Experimental BoxLite micro-VM Bash backend.

Every command is an ephemeral BoxLite micro-VM with a *single* explicit
read-write workspace volume and disabled guest networking. This keeps current
Ridge Code's Bash-only sandbox policy: host Read/Write/MCP/Skill/Team tools
remain unreachable in VM mode. Never fall back to host subprocess or Docker.

The upstream BoxLite SDK is optional and async-native. The synchronous Agent
tool owner thread runs a scoped asyncio loop per invocation. A turn interrupt
cancels the guest exec, then exits the BoxLite async context to release the VM.
Guest cleanup is best effort under a broken or uninterruptible SDK; this is
an opt-in local single-operator backend, not a multi-tenant security claim.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import secrets
import sys
from pathlib import Path
from threading import Event
from typing import Any, Callable

from core.sandbox.contracts import CommandResult, SandboxUnavailable
from core.sandbox.boxlite_pool import BoxliteSessionPool
from core.sandbox.docker_lifecycle import installation_key_path

_IMAGE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._/:@+-]{0,199}\Z")


class BoxliteCommandProvider:
    kind = "boxlite"

    def __init__(
        self,
        workspace: str | Path,
        *,
        image: str = "python:3.12-slim",
        memory_mib: int = 1024,
        cpus: int = 2,
        box_factory: Callable[..., Any] | None = None,
        network_factory: Callable[..., Any] | None = None,
        session_pool: BoxliteSessionPool | None = None,
        session_scope: str | None = None,
    ) -> None:
        if sys.platform not in ("linux", "darwin"):
            raise SandboxUnavailable("BoxLite micro-VM requires a supported Linux/macOS host")
        try:
            self.workspace = Path(workspace).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxUnavailable("Invalid VM workspace") from exc
        if not self.workspace.is_dir():
            raise SandboxUnavailable("VM workspace must be an existing directory")
        if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image) or ".." in image:
            raise SandboxUnavailable("Invalid BoxLite image")
        if (type(memory_mib) is not int or not 128 <= memory_mib <= 16384
                or type(cpus) is not int or not 1 <= cpus <= 8):
            raise SandboxUnavailable("Invalid BoxLite VM resource limits")
        # BoxLite host-path volume mounts are RW. Refuse to expose the local
        # Docker ownership key through the shared workspace or a symlink alias.
        key = installation_key_path().resolve(strict=False)
        try:
            key.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            raise SandboxUnavailable("VM workspace would expose sandbox owner secrets")
        if box_factory is None or network_factory is None:
            try:
                from boxlite import NetworkSpec, SimpleBox
            except ImportError as exc:
                raise SandboxUnavailable(
                    "BoxLite SDK is not installed; install optional 'boxlite' "
                    "and configure hardware virtualization"
                ) from exc
            box_factory = box_factory or SimpleBox
            network_factory = network_factory or NetworkSpec
        self._box_factory = box_factory
        self._network_factory = network_factory
        self.image = image
        self.memory_mib = memory_mib
        self.cpus = cpus
        if (session_pool is None) != (session_scope is None):
            raise ValueError("VM pool and session scope must be supplied together")
        self._session_pool = session_pool
        self._session_scope = session_scope
        self._owns_pool = False

    def enable_warm_pool(
        self, *, session_scope: str, max_replicas: int = 1,
        idle_timeout: float = 60,
    ) -> None:
        """Opt-in single-Agent scoped warm VM reuse; never share across agents."""
        if self._session_pool is not None:
            raise ValueError("VM session pool is already configured")
        if not isinstance(session_scope, str) or not session_scope:
            raise ValueError("A VM session identity is required")
        self._session_pool = BoxliteSessionPool(
            self._make_box,
            max_replicas=max_replicas, idle_timeout=idle_timeout,
        )
        self._session_scope = session_scope
        self._owns_pool = True

    def close(self) -> None:
        """Release guests and private loop during Agent shutdown."""
        if self._owns_pool and self._session_pool is not None:
            self._session_pool.shutdown()

    def _working_dir(self, directory: str) -> str:
        if not isinstance(directory, str) or "\x00" in directory:
            raise ValueError("Directory must be a valid path string")
        try:
            path = Path(directory)
            target = (path if path.is_absolute() else self.workspace / path).resolve(strict=True)
            relative = target.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionError("Working directory leaves VM workspace") from exc
        except (OSError, RuntimeError) as exc:
            raise ValueError("Working directory is unavailable or invalid") from exc
        if not target.is_dir():
            raise ValueError("Working directory is not a directory")
        return "/workspace" if relative == Path(".") else "/workspace/" + relative.as_posix()

    def _make_box(self) -> Any:
        """Do not expose options construction to model/tool arguments."""
        return self._box_factory(
            image=self.image,
            name=f"ridgecode-{secrets.token_hex(10)}",
            auto_remove=True,
            memory_mib=self.memory_mib,
            cpus=self.cpus,
            volumes=[(str(self.workspace), "/workspace", False)],
            network=self._network_factory(mode="disabled"),
            working_dir="/workspace",
        )

    async def _execute_guest(
        self, box: Any, command: str, cwd: str, timeout: float,
        cancel_event: Event | None,
    ) -> CommandResult:
        if cancel_event is not None and cancel_event.is_set():
            return CommandResult("", "", None, interrupted=True)
        task = asyncio.create_task(box.exec(
            "sh", "-lc", command, cwd=cwd,
            user=str(os.getuid() or 65534), timeout=timeout,
            env={"HOME": "/tmp"},
        ))
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while not task.done():
                if cancel_event is not None and cancel_event.is_set():
                    task.cancel()
                    return CommandResult("", "", None, interrupted=True)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    task.cancel()
                    return CommandResult("", "", None, timed_out=True)
                await asyncio.wait({task}, timeout=min(0.1, remaining))
            if cancel_event is not None and cancel_event.is_set():
                return CommandResult("", "", None, interrupted=True)
            result = task.result()
            return CommandResult(
                str(result.stdout or ""), str(result.stderr or ""),
                result.exit_code,
            )
        finally:
            if not task.done():
                task.cancel()

    async def _run_in_vm(
        self, command: str, cwd: str, timeout: float, cancel_event: Event | None,
    ) -> CommandResult:
        async with self._make_box() as box:
            return await self._execute_guest(box, command, cwd, timeout, cancel_event)

    def execute(
        self,
        command: str,
        directory: str = ".",
        timeout_seconds: float = 120,
        *,
        cancel_event: Event | None = None,
    ) -> CommandResult:
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError("Command must be a nonempty string without null bytes")
        if (isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 600):
            raise ValueError("VM command timeout is out of range")
        cwd = self._working_dir(directory)
        if cancel_event is not None and cancel_event.is_set():
            return CommandResult("", "", None, interrupted=True)
        try:
            if self._session_pool is not None:
                assert self._session_scope is not None
                self._session_pool.acquire(self._session_scope)
                try:
                    return self._session_pool.execute(
                        self._session_scope,
                        lambda box: self._execute_guest(
                            box, command, cwd, timeout_seconds, cancel_event,
                        ),
                        timeout_seconds=timeout_seconds,
                    )
                finally:
                    self._session_pool.release(self._session_scope)
            return asyncio.run(self._run_in_vm(
                command, cwd, timeout_seconds, cancel_event,
            ))
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise SandboxUnavailable(
                "BoxLite VM startup, command or shutdown failed; host fallback forbidden"
            ) from exc
