"""Launch the actual Bun/OpenTUI app inside a POSIX pseudoterminal.

Unlike testRender, this exercises PTY terminal-size negotiation, keyboard
input, and renderer teardown. All HTTP requests target a fake loopback server;
no API key, real agent or network access is required.
"""
from __future__ import annotations

import fcntl
import http.server
import json
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path


class MockBackend(http.server.BaseHTTPRequestHandler):
    calls: list[str] = []
    forked = False
    fork_target: str | None = None

    def log_message(self, format, *args):
        pass

    def respond(self, payload, status=200):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.calls.append("GET " + self.path)
        if self.path == "/api/health":
            return self.respond({"status": "ok"})
        if self.path == "/api/sessions":
            return self.respond([])
        if self.path.endswith("/history"):
            return self.respond({"messages": [
                {"role": "user", "content": "PTY-tree-root"},
            ] if self.forked else []})
        if self.path.endswith("/tree"):
            return self.respond({
                "nodes": {
                    "a1b2c3d4": {
                        "id": "a1b2c3d4", "type": "message",
                        "role": "user", "parentId": None, "content": "PTY-tree-root",
                    },
                    "b1b2c3d4": {
                        "id": "b1b2c3d4", "type": "message",
                        "role": "assistant", "parentId": "a1b2c3d4",
                        "content": "Previous answer",
                    },
                },
                "children": {"__root__": ["a1b2c3d4"],
                             "a1b2c3d4": ["b1b2c3d4"]},
                "cursor_id": "b1b2c3d4",
                "labels": {},
            })
        if self.path.endswith("/turn"):
            return self.respond({"status": "idle"})
        return self.respond({"detail": "missing"}, 404)

    def do_POST(self):
        self.calls.append("POST " + self.path)
        if self.path == "/api/sessions":
            return self.respond({"id": "deadbeef1234", "title": "PTY", "busy": False}, 201)
        if self.path.endswith("/activate"):
            return self.respond({"status": "active"})
        if self.path.endswith("/fork"):
            size = int(self.headers.get("Content-Length", "0"))
            if size > 1024:
                return self.respond({"detail": "too large"}, 413)
            body = json.loads(self.rfile.read(size))
            if body.get("target_id") != "a1b2c3d4":
                return self.respond({"detail": "node not found"}, 404)
            type(self).forked = True
            type(self).fork_target = body["target_id"]
            return self.respond({"cursor_id": body["target_id"]})
        return self.respond({"status": "ok"})


def main():
    root = Path(__file__).resolve().parents[1]
    backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockBackend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 108, 0, 0))
    env = os.environ.copy()
    env["RIDGECODE_SERVER"] = "http://127.0.0.1:" + str(backend.server_port)
    env.pop("RIDGECODE_AUTH_TOKEN", None)
    env.pop("WODEAGENT_AUTH_TOKEN", None)
    proc = None
    captured = bytearray()
    try:
        proc = subprocess.Popen(
            ["bun", "run", "src/index.tsx"],
            cwd=root,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        slave = -1

        def pump(expected: bytes, timeout: float) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                readable, _, _ = select.select([master], [], [], 0.15)
                if readable:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    captured.extend(chunk)
                    if len(captured) > 1000000:
                        del captured[:-200000]
                    if expected in captured:
                        return True
            return expected in captured

        assert pump(b"Ridge", 12.0), "OpenTUI did not render in the PTY"
        assert "GET /api/health" in MockBackend.calls, "TUI did not contact the local Agent API"
        # Wait until the composer is painted, not just the header.
        assert pump(b"/help for commands", 4.0), "Composer did not render"
        time.sleep(0.2)
        # Real keyboards deliver separate bytes; a single write can be parsed
        # as one unrecognized escape/input frame by terminal libraries.
        for key in (47, 104, 101, 108, 112, 13):  # /help + Enter
            os.write(master, bytes([key]))
            time.sleep(0.06)
        if not pump(b"/fork", 5.0):
            print("PTY tail:", repr(bytes(captured[-5000:])))
            print("Fixture routes:", MockBackend.calls)
            raise AssertionError("PTY keyboard or terminal command help not rendered")

        def type_command(command: str) -> None:
            for byte in (command + "\r").encode("ascii"):
                os.write(master, bytes([byte]))
                time.sleep(0.04)

        type_command("/tree")
        assert pump(b"PTY-tree-root", 5.0), "Tree command did not display actual server nodes"
        assert "GET /api/sessions/deadbeef1234/tree" in MockBackend.calls, "Tree endpoint unused"

        type_command("/fork a1b2c3d4")
        if not pump(b"Forked from message", 5.0):
            print("PTY tail:", repr(bytes(captured[-3000:])))
            print("Fixture routes:", MockBackend.calls)
            raise AssertionError("Fork HTTP flow did not update TUI notice")
        assert MockBackend.fork_target == "a1b2c3d4", "Fork target was not sent to API"
        assert MockBackend.calls.count("GET /api/sessions/deadbeef1234/history") >= 2, (
            "Fork did not refresh selected branch history"
        )

        os.write(master, b"\x03")  # Ctrl+C; idle UI must quit cleanly.
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            raise AssertionError("Ctrl+C did not shut down OpenTUI")
        assert proc.returncode == 0, "OpenTUI exited with error: " + str(proc.returncode)
        print("PTY smoke: renderer, /help, /tree, /fork HTTP, Ctrl+C exit OK")
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        os.close(master)
        if slave >= 0:
            os.close(slave)
        backend.shutdown()
        backend.server_close()


if __name__ == "__main__":
    main()
