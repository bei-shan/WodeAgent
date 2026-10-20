"""Bounded, cancellable subprocess execution for Bash and Docker CLI.

Drain both child pipes concurrently while retaining a strictly bounded
number of bytes per stream (128 KiB by default). This is output resource control, not process or network isolation.
POSIX launches its own process group; Windows assigns children to a kill-on-close Job Object.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any


MAX_CAPTURE_BYTES = 131_072
_READ_CHUNK = 16_384


@dataclass(frozen=True)
class ProcessOutcome:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    interrupted: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def _kill_tree(proc: subprocess.Popen[Any], job: Any = None) -> None:
    # A completed shell may have living grandchildren; kill its Job Object
    # even when the direct child has already exited.
    if job is not None:
        try:
            job.terminate()
            return
        except OSError:
            pass
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


def _bounded_reader(pipe: Any, retained: bytearray, overflow: list[bool],
                    max_bytes: int) -> None:
    """Drain until EOF so full pipes never block the child; discard overflow."""
    try:
        while True:
            chunk = os.read(pipe.fileno(), _READ_CHUNK)
            if not chunk:
                return
            left = max_bytes - len(retained)
            if left > 0:
                retained.extend(chunk[:left])
            if len(chunk) > left:
                overflow[0] = True
    except (OSError, ValueError):
        # The owner closes pipes after a bounded wait on cancellation.
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
    max_output_bytes: int = MAX_CAPTURE_BYTES,
) -> ProcessOutcome:
    """Execute with a hard deadline, live cancellation and bounded pipe memory.

    Reader threads are daemonized because a malicious process can create an
    out-of-group descendant holding a pipe open. We never wait indefinitely
    for a descendant after the direct process has been reaped.
    """
    if stdin_data is not None and not isinstance(stdin_data, bytes):
        raise ValueError("stdin_data must be bytes")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or not 1 <= max_output_bytes <= 1_048_576:
        raise ValueError("max_output_bytes must be between 1 and 1048576")
    if cancel_event is not None and cancel_event.is_set():
        return ProcessOutcome("", "", None, interrupted=True)
    opts: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}
    job = None
    if os.name == "nt":
        from core.runtime.windows_process_job import WindowsProcessJob
        # Set up containment BEFORE spawning. If unavailable, no command runs.
        job = WindowsProcessJob()
        opts["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(
            args, shell=shell, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            **opts,
        )
    except BaseException:
        if job is not None:
            job.close()
        raise
    if job is not None:
        try:
            job.assign(process.pid)
        except OSError as exc:
            # Do not silently downgrade to killing only the direct process.
            job.close()
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            raise OSError("Windows Job Object assignment failed; refusing uncontained execution") from exc
    captured = (bytearray(), bytearray())
    overflow = ([False], [False])
    streams = (process.stdout, process.stderr)
    readers = [
        Thread(target=_bounded_reader, args=(pipe, buf, flag, max_output_bytes),
               name="ridge-process-pipe", daemon=True)
        for pipe, buf, flag in zip(streams, captured, overflow)
    ]
    for worker in readers:
        worker.start()
    if stdin_data is not None and process.stdin is not None:
        Thread(target=_write_stdin, args=(process.stdin, stdin_data),
               name="ridge-process-stdin", daemon=True).start()

    deadline = time.monotonic() + timeout
    timed_out = interrupted = False
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
        _kill_tree(process, job)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        if timed_out or interrupted:
            _kill_tree(process, job)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _kill_tree(process, job)

        # Closing the last Job Object handle kills any remaining descendants
        # even when the shell already completed successfully.
        if job is not None:
            job.close()

        # The process may have spawned a detached child holding stdout open.
        # Read what arrived, then close our descriptors without an unbounded
        # communicate()/join() blocking on that untrusted descendant.
        for worker in readers:
            worker.join(timeout=1 if timed_out or interrupted else 2)
        for stream in streams:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    return ProcessOutcome(
        captured[0].decode("utf-8", errors="replace"),
        captured[1].decode("utf-8", errors="replace"),
        process.returncode,
        timed_out=timed_out, interrupted=interrupted,
        stdout_truncated=overflow[0][0], stderr_truncated=overflow[1][0],
    )
