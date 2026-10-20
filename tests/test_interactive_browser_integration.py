"""Real Chromium loopback smoke. Skips core CI unless Playwright is installed."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from core.config import Config
from tools.builtin.browser_use import BrowserUseTool


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/next":
            content = "<html><title>Next</title><body><h1>Next page</h1></body></html>"
        else:
            content = """
            <html><title>Browser use smoke</title><body>
            <h1>Home page</h1>
            <label>Name <input placeholder="Type your name" /></label>
            <button onclick="document.querySelector('h1').innerText = 'Clicked'">Click me</button>
            <a href="/next">Next page</a>
            </body></html>
            """
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content.encode())))
        self.end_headers()
        self.wfile.write(content.encode())

    def log_message(self, *args):
        pass


def test_real_browser_tool_navigation_and_refs(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    tool = BrowserUseTool(tmp_path, Config(
        browser_use_enabled=True, browser_use_allow_private_addresses=True,
    ))
    try:
        base = f"http://127.0.0.1:{server.server_port}/"

        def call(**kwargs):
            result = json.loads(tool.run(kwargs))
            assert result["status"] == "success", result
            return result["data"]

        snapshot = call(action="open", url=base)
        assert snapshot["title"] == "Browser use smoke"
        assert "Home page" in snapshot["page_text"]
        text_ref = next(el["ref"] for el in snapshot["elements"]
                        if el["tag"] == "input")
        snapshot = call(action="fill", ref=text_ref, text="Alice")
        next_ref = next(el["ref"] for el in snapshot["elements"]
                       if el["label"] == "Next page")
        snapshot = call(action="click", ref=next_ref)
        assert snapshot["url"].endswith("/next")
        assert "Next page" in snapshot["page_text"]
        snapshot = call(action="back")
        assert snapshot["url"] == base
        snapshot = call(action="screenshot")
        assert Path(snapshot["screenshot_path"]).is_file()
        call(action="close")
    finally:
        tool.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
