"""Optional authenticated Docker-only interactive terminal.

The Docker daemon allocates a real guest TTY. The host process merely relays
PTY bytes over a WebSocket, never runs the user's command in a host shell.
Disabled by default and deliberately unavailable without WEB_AUTH_TOKEN.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import subprocess
import termios
import time

from fastapi import WebSocket, WebSocketDisconnect
from core.sandbox.docker_provider import DockerCommandProvider

logger = logging.getLogger(__name__)
MAX_TERMINAL_SECONDS = 600
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


async def serve_docker_terminal(websocket: WebSocket, provider: DockerCommandProvider) -> None:
    """Single bounded terminal lifetime, closing both host CLI and guest."""
    argv, name = provider.command_argv(
        "exec /bin/sh -i", timeout_seconds=MAX_TERMINAL_SECONDS,
    )
    argv[2:2] = ["-i", "-t"]
    master, slave = pty.openpty()
    proc: subprocess.Popen | None = None
    lease_active = False
    reader_task: asyncio.Task | None = None
    try:
        provider._leases.activate(name)
        lease_active = True
        try:
            proc = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True, cwd=str(provider.workspace),
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"},
            )
        finally:
            os.close(slave)
        await websocket.accept()
        await websocket.send_json({"type": "ready", "backend": "docker", "interactive": True})
        total_output = 0
        started = time.monotonic()

        async def relay_output() -> None:
            nonlocal total_output
            while True:
                try:
                    chunk = await asyncio.to_thread(os.read, master, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                total_output += len(chunk)
                if total_output > MAX_OUTPUT_BYTES:
                    await websocket.send_json({"type": "limit", "message": "Output budget exceeded"})
                    return
                await websocket.send_json({"type": "data", "text": chunk.decode("utf-8", "replace")})

        async def relay_input() -> None:
            total_input = 0
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "resize":
                    cols, rows = message.get("cols"), message.get("rows")
                    if type(cols) is int and type(rows) is int and 20 <= cols <= 300 and 5 <= rows <= 120:
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                        if proc is not None and proc.poll() is None:
                            os.killpg(proc.pid, signal.SIGWINCH)
                    continue
                if message.get("type") != "input":
                    continue
                data = message.get("text")
                if not isinstance(data, str):
                    continue
                payload = data.encode("utf-8")
                if not payload or len(payload) > 4096:
                    continue
                total_input += len(payload)
                if total_input > 128 * 1024:
                    await websocket.send_json({"type": "limit", "message": "Input budget exceeded"})
                    return
                await asyncio.to_thread(os.write, master, payload)

        reader_task = asyncio.create_task(relay_output())
        writer_task = asyncio.create_task(relay_input())
        try:
            done, pending = await asyncio.wait(
                {reader_task, writer_task}, timeout=MAX_TERMINAL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                try:
                    await task
                except (WebSocketDisconnect, asyncio.CancelledError, OSError):
                    pass
            if not done:
                await websocket.send_json({"type": "limit", "message": "Terminal lifetime exceeded"})
        finally:
            writer_task.cancel()
            reader_task.cancel()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Docker terminal failed to start: %s", type(exc).__name__)
        try:
            await websocket.send_json({"type": "error", "message": "Sandbox terminal is unavailable"})
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if lease_active:
            await asyncio.to_thread(provider._force_remove, name)
            await asyncio.to_thread(provider._leases.deactivate, name)
        try:
            os.close(master)
        except OSError:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
