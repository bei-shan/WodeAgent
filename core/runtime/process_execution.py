"""Cancellable subprocess runner for in-process Bash execution.

On POSIX spawn a new session and terminate the process group on cancellation
or timeout so shell grandchildren do not survive as orphaned jobs.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from threading import Event
from typing import Any


@dataclass(frozen=True)
class ProcessOutcome:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    interrupted: bool = False


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _kill_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def run_process(
    args: str | list[str],
    *,
    shell: bool,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    cancel_event: Event | None = None,
    poll_interval: float = 0.1,
    stdin_data: bytes | None = None,
) -> ProcessOutcome:
    """Run a process with a hard deadline and optional cooperative interrupt.

    No external stdout/stderr callbacks are invoked on worker threads.
    Cancellation is scoped to one invocation, not a global process list.
    """
    if stdin_data is not None and not isinstance(stdin_data, bytes):
        raise ValueError("stdin_data must be bytes")
    if cancel_event is not None and cancel_event.is_set():
        return ProcessOutcome("", "", None, interrupted=True)
    opts: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}
    # On Windows, start a new process group so a future dedicated Job Object
    # implementation can terminate descendants reliably.
    if os.name == "nt":
        opts["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        args, shell=shell, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_data is not None else None,
        text=stdin_data is None,
        **opts,
    )
    input_pending = stdin_data
    deadline = time.monotonic() + timeout
    timed_out = False
    interrupted = False
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                interrupted = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            wait = min(remaining, poll_interval) if cancel_event is not None else remaining
            try:
                try:
                    stdout, stderr = process.communicate(
                        input=input_pending, timeout=wait,
                    )
                finally:
                    # CPython's communicate resumes pending pipe writes after
                    # a TimeoutExpired; resending the bytes would duplicate
                    # data or raise ValueError on the second invocation.
                    input_pending = None
                return ProcessOutcome(_as_text(stdout), _as_text(stderr), process.returncode)
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        _kill_tree(process)
        try:
            process.communicate(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise
    _kill_tree(process)
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired as exc:
        # A descendant may have escaped its process group while retaining
        # inherited pipe descriptors. Do not block a cancelled turn forever.
        stdout, stderr = exc.stdout, exc.stderr
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
    return ProcessOutcome(
        _as_text(stdout), _as_text(stderr), process.returncode,
        timed_out=timed_out, interrupted=interrupted,
    )
