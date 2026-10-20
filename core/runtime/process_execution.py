"""Cancellable subprocess runner with *bounded* stdout/stderr memory.

A subprocess can print gigabytes or never close stdout. Reader threads keep
pipes drained while retaining only a small capped prefix in Python memory.
Wall-clock timeout and cancellation terminate the POSIX process group.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any


DEFAULT_CAPTURE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ProcessOutcome:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    interrupted: bool = False
    # Byte counts are totals drained from the OS pipes, not bytes retained.
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    output_truncated: bool = False


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self.buffer = bytearray()
        self.total_bytes = 0

    def drain(self, pipe: Any) -> None:
        try:
            while True:
                chunk = os.read(pipe.fileno(), 8192)
                if not chunk:
                    return
                self.total_bytes += len(chunk)
                room = self._limit - len(self.buffer)
                if room > 0:
                    self.buffer.extend(chunk[:room])
        finally:
            pipe.close()

    def text(self) -> str:
        return bytes(self.buffer).decode("utf-8", errors="replace")

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self.buffer)


def _kill_tree(proc: subprocess.Popen[Any]) -> None:
    if os.name == "posix":
        # Do not guard with poll(): a shell may have already exited while
        # its descendants still hold output pipes and remain in the group.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    elif os.name == "nt" and proc.poll() is None:
        # Job Objects are the long-term Windows solution. This best-effort
        # fallback also terminates descendants, unlike Popen.kill() alone.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, timeout=2, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _write_stdin(pipe: Any, data: bytes) -> None:
    try:
        pipe.write(data)
        pipe.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
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
    max_output_bytes: int = DEFAULT_CAPTURE_BYTES,
) -> ProcessOutcome:
    """Execute with bounded in-memory output and cancellation propagation.

    Each output stream is capped independently. Excess bytes are drained and
    discarded rather than accumulated in `communicate()` buffers. The output
    byte counts reflect what was observed, including discarded bytes.
    """
    if stdin_data is not None and not isinstance(stdin_data, bytes):
        raise ValueError("stdin_data must be bytes")
    if type(max_output_bytes) is not int or max_output_bytes < 1:
        raise ValueError("max_output_bytes must be a positive integer")
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("timeout and poll_interval must be positive")
    if cancel_event is not None and cancel_event.is_set():
        return ProcessOutcome("", "", None, interrupted=True)

    opts: dict[str, Any] = (
        {"start_new_session": True} if os.name == "posix" else {}
    )
    if os.name == "nt":
        # No child code may run before the Job Object owns it.
        opts["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004  # CREATE_SUSPENDED
        )
    process = subprocess.Popen(
        args, shell=shell, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        bufsize=0, **opts,
    )
    # A Windows shell may spawn children which outlive the shell itself.
    # Bind immediately and fail closed if the host refuses Job assignment.
    job = None
    if os.name == "nt":
        try:
            from core.runtime.windows_job import WindowsProcessJob
            job = WindowsProcessJob(int(process._handle))
            job.resume(process.pid)
        except BaseException:
            _kill_tree(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_tree(process)
            raise
    assert process.stdout is not None and process.stderr is not None
    out = _BoundedCapture(max_output_bytes)
    err = _BoundedCapture(max_output_bytes)
    readers = [
        Thread(target=out.drain, args=(process.stdout,), daemon=True,
               name="ridge-subprocess-stdout"),
        Thread(target=err.drain, args=(process.stderr,), daemon=True,
               name="ridge-subprocess-stderr"),
    ]
    for reader in readers:
        reader.start()

    if stdin_data is not None:
        assert process.stdin is not None
        Thread(
            target=_write_stdin, args=(process.stdin, stdin_data),
            daemon=True, name="ridge-subprocess-stdin",
        ).start()

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
            try:
                process.wait(timeout=min(remaining, poll_interval))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        _kill_tree(process)
        raise
    finally:
        if timed_out or interrupted:
            if job is not None:
                job.terminate()
            else:
                _kill_tree(process)
        if process.poll() is None:
            # A process can have exited between checks; never leave a child
            # running if the wait loop itself raised.
            _kill_tree(process)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
        # A grandchild can inherit pipes after its leader exits. Never wait
        # forever for a descendant that is outside our process group.
        for reader in readers:
            reader.join(timeout=0.3)
        if any(reader.is_alive() for reader in readers):
            # The foreground shell may have exited, but an inherited stdout
            # handle on a background grandchild is still writable. Terminate
            # the owned process group, then give the pipe drains a final
            # bounded opportunity to reach EOF. Never wait indefinitely.
            _kill_tree(process)
            for reader in readers:
                reader.join(timeout=0.3)
        if job is not None:
            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE reaps descendants even
            # after the root command has already exited.
            job.close()

    return ProcessOutcome(
        out.text(), err.text(), process.returncode,
        timed_out=timed_out, interrupted=interrupted,
        stdout_bytes=out.total_bytes, stderr_bytes=err.total_bytes,
        output_truncated=out.truncated or err.truncated,
    )
