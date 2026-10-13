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
            return self.respond({"messages": []})
        if self.path.endswith("/turn"):
            return self.respond({"status": "idle"})
        return self.respond({"detail": "missing"}, 404)

    def do_POST(self):
        self.calls.append("POST " + self.path)
        if self.path == "/api/sessions":
            return self.respond({"id": "deadbeef1234", "title": "PTY", "busy": False}, 201)
        if self.path.endswith("/activate"):
            return self.respond({"status": "active"})
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
        for key in b"/help\\r".replace(b"\\\\r", b"\\r"):
            os.write(master, bytes([key]))
            time.sleep(0.06)
        if not pump(b"/fork", 5.0):
            print("PTY tail:", repr(bytes(captured[-5000:])))
            print("Fixture routes:", MockBackend.calls)
            raise AssertionError("PTY keyboard or terminal command help not rendered")

        os.write(master, b"\x03")  # Ctrl+C; idle UI must quit cleanly.
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            raise AssertionError("Ctrl+C did not shut down OpenTUI")
        assert proc.returncode == 0, "OpenTUI exited with error: " + str(proc.returncode)
        print("PTY smoke: renderer, server bootstrap, slash command, Ctrl+C exit OK")
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
