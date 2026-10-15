"""Experimental E2B remote micro-VM Bash provider (single trusted gateway only).

There is deliberately NO local workspace mount or implicit file sync. The
remote sandbox owns its filesystem and credentials. Multi-gateway operation,
remote discovery/adoption, and automatic orphan deletion are NOT implemented;
they require shared fenced ownership and capacity coordination first.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core.sandbox.contracts import CommandResult, SandboxUnavailable

_TEMPLATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_OUTPUT = 51200


class E2BCapacity:
    """Conservative per-process VM slot ledger: uncertain destroys retain slots."""

    def __init__(self, limit: int):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("E2B capacity must be an integer between 1 and 16")
        self.limit = limit
        self._tokens: set[str] = set()
        self._lock = threading.Lock()

    def reserve(self, token: str) -> None:
        with self._lock:
            if token in self._tokens or len(self._tokens) >= self.limit:
                raise SandboxUnavailable("E2B VM capacity exhausted or reserved")
            self._tokens.add(token)

    def release(self, token: str) -> None:
        with self._lock:
            self._tokens.discard(token)

    @property
    def used(self) -> int:
        with self._lock:
            return len(self._tokens)


_CAPACITIES: dict[str, E2BCapacity] = {}
_CAPACITY_LOCK = threading.Lock()


def local_e2b_capacity(deployment: str, limit: int) -> E2BCapacity:
    """One hard cap per *process*. Never represent this as multi-process safety."""
    with _CAPACITY_LOCK:
        found = _CAPACITIES.get(deployment)
        if found is not None:
            if found.limit != limit:
                raise SandboxUnavailable("Conflicting E2B capacity configuration")
            return found
        created = E2BCapacity(limit)
        _CAPACITIES[deployment] = created
        return created


class E2BCommandProvider:
    kind = "e2b"
    remote_workspace = "/home/user"

    def __init__(
        self,
        workspace: str | Path,
        *,
        api_key: str | None = None,
        template: str = "code-interpreter-v1",
        sandbox_ttl: int = 300,
        capacity_limit: int = 2,
        single_gateway_ack: bool = False,
        sdk_factory: Callable[..., Any] | None = None,
        capacity: E2BCapacity | None = None,
    ):
        # Hard fail-closed selection. Operators must consciously acknowledge
        # that local admission limits do not cover other gateway processes.
        if single_gateway_ack is not True:
            raise SandboxUnavailable("E2B requires explicit single-gateway acknowledgement")
        key = api_key if api_key is not None else os.environ.get("E2B_API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise SandboxUnavailable("E2B API credential is required")
        if not isinstance(template, str) or not _TEMPLATE.fullmatch(template):
            raise ValueError("Invalid E2B template")
        if type(sandbox_ttl) is not int or not 120 <= sandbox_ttl <= 3600:
            raise ValueError("E2B VM TTL must be 120–3600 seconds")
        try:
            self.workspace = Path(workspace).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxUnavailable("Invalid local project root") from exc
        if not self.workspace.is_dir():
            raise SandboxUnavailable("Project root is not a directory")
        if sdk_factory is None:
            try:
                from e2b_code_interpreter import Sandbox
            except ImportError as exc:
                raise SandboxUnavailable("Optional e2b-code-interpreter SDK is missing") from exc
            sdk_factory = Sandbox.create
        self._create = sdk_factory
        self._key = key
        self._template = template
        self._ttl = sandbox_ttl
        self._slot = secrets.token_hex(24)
        self._owner = secrets.token_hex(24)
        self._capacity = capacity or local_e2b_capacity("e2b", capacity_limit)
        self._lock = threading.RLock()
        self._busy = False
        self._closed = False
        self._poisoned = False
        self._client: Any = None
        self._reserved = False
        self._abort = threading.Event()

    def _bounded_call(
        self, fn: Callable[[], Any], deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, Any]:
        """Execute blocking SDK calls in daemon workers with a hard caller budget.

        If a remote SDK call hangs, retain admission capacity and poison this
        provider. A late Sandbox.create result must be destroyed by its worker.
        """
        done = threading.Event()
        outcome: list[Any] = []

        def worker() -> None:
            try:
                value = fn()
                if self._abort.is_set():
                    # Only called for creation, NOT for a completed command.
                    # No implicit replay or remote adoption after caller stop.
                    if self._client is None and hasattr(value, "kill"):
                        try:
                            killed = value.kill()
                            if killed is False:
                                pass  # already gone is also a definitive outcome
                        except Exception:
                            pass
                outcome.append((True, value))
            except BaseException as exc:
                outcome.append((False, exc))
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True, name="ridge-e2b-sdk").start()
        while not done.wait(timeout=0.05):
            if (self._abort.is_set() or
                    (cancel_event is not None and cancel_event.is_set())):
                self._abort.set()
                self._poisoned = True
                return "cancel", None
            if time.monotonic() >= deadline:
                self._abort.set()
                self._poisoned = True
                return "timeout", None
        ok, value = outcome[0]
        if not ok:
            return "error", value
        return "ok", value

    def _ensure_remote(self, cancel_event: threading.Event | None) -> None:
        if self._client is not None:
            return
        self._capacity.reserve(self._slot)
        self._reserved = True

        def create() -> Any:
            return self._create(
                template=self._template,
                timeout=self._ttl,
                api_key=self._key,
                allow_internet_access=False,
                metadata={
                    "ridge_provider": "e2b_experimental",
                    "ridge_owner": self._owner,
                    "ridge_session": self._slot,
                },
            )
        status, value = self._bounded_call(
            create, time.monotonic() + 45, cancel_event,
        )
        if status == "ok":
            if self._abort.is_set():
                self._poisoned = True
                raise SandboxUnavailable("E2B create returned after cancellation")
            self._client = value
            return
        if status == "error":
            # Failed create may still have remote side effects. Retain the
            # reservation: never falsely reissue the slot without evidence.
            self._poisoned = True
        raise SandboxUnavailable("E2B VM creation failed or was interrupted")

    def _kill_remote(self) -> bool:
        client = self._client
        if client is None:
            return False
        # SDK may be network-blocked: deadline failure retains capacity.
        result: list[tuple[bool, Any]] = []
        done = threading.Event()
        def worker():
            try:
                result.append((True, client.kill()))
            except Exception:
                result.append((False, None))
            finally:
                done.set()
        threading.Thread(target=worker, daemon=True, name="ridge-e2b-kill").start()
        if not done.wait(5):
            self._poisoned = True
            return False
        if not result or not result[0][0]:
            self._poisoned = True
            return False
        # A False return means the VM was already absent in the control plane.
        self._client = None
        if self._reserved:
            self._capacity.release(self._slot)
            self._reserved = False
        return True

    def execute(
        self, command: str, directory: str = ".", timeout_seconds: float = 120,
        *, cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        import math
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError("Invalid E2B Bash command")
        if not isinstance(directory, str) or directory not in (".", "/home/user"):
            raise PermissionError("E2B has no local workspace mount or host directory mapping")
        if (type(timeout_seconds) not in (int, float) or
                not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600):
            raise ValueError("Invalid E2B command timeout")
        if cancel_event is not None and cancel_event.is_set():
            return CommandResult("", "", None, interrupted=True)
        with self._lock:
            if self._closed or self._poisoned or self._busy:
                raise SandboxUnavailable("E2B session closed, busy, or cleanup unconfirmed")
            self._busy = True
            try:
                self._ensure_remote(cancel_event)
                client = self._client
                def invoke():
                    # The command runs in a REMOTE VM. No host shell, volume,
                    # Docker daemon or implicit local file synchronization.
                    return client.commands.run(
                        command, cwd=self.remote_workspace,
                        timeout=timeout_seconds,
                    )
                status, value = self._bounded_call(
                    invoke, time.monotonic() + timeout_seconds + 2, cancel_event,
                )
                if status in ("cancel", "timeout"):
                    # No speculative replay after an ambiguous remote outcome.
                    self._poisoned = True
                    self._kill_remote()
                    return CommandResult(
                        "", "", None, timed_out=status == "timeout",
                        interrupted=status == "cancel",
                    )
                if status == "error":
                    # E2B may raise CommandExitException for non-zero exit;
                    # preserve such legitimate command results only when
                    # stdout/stderr and exit_code are all provided.
                    if all(hasattr(value, field) for field in
                           ("stdout", "stderr", "exit_code")):
                        pass
                    else:
                        self._poisoned = True
                        self._kill_remote()
                        raise SandboxUnavailable("Remote command outcome is unknown")
                if not isinstance(getattr(value, "exit_code", None), int):
                    self._poisoned = True
                    self._kill_remote()
                    raise SandboxUnavailable("Invalid E2B command result")
                stdout, stderr = getattr(value, "stdout", ""), getattr(value, "stderr", "")
                if not isinstance(stdout, str) or not isinstance(stderr, str):
                    self._poisoned = True
                    self._kill_remote()
                    raise SandboxUnavailable("Invalid E2B command output")
                return CommandResult(stdout[:_MAX_OUTPUT], stderr[:_MAX_OUTPUT], value.exit_code)
            finally:
                self._busy = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._abort.set()
            if self._client is not None:
                self._kill_remote()
            # A create timeout or unknown remote deletion never releases the
            # reserved slot. Operator reconciliation is required.

    @property
    def capacity_used(self) -> int:
        return self._capacity.used
