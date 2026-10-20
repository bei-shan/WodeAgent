"""Real Chromium browser acceptance; local ephemeral HTTP servers only.

No model key or internet is needed. The browser is real: routes, cookies,
clicks, redirected requests and two independent browser profiles are tested.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import pytest

from core.browser_session import BrowserSession
from core.browser_url_policy import BrowserURLPolicy

# The broad regression lane intentionally does not install optional Playwright.
# The dedicated live Chromium CI lane DOES install it and rejects ANY skips.
pytest.importorskip("playwright.sync_api", reason="requires optional real Chromium integration lane")


@pytest.fixture
def sites():
    evil_hits = []
    class Evil(BaseHTTPRequestHandler):
        def do_GET(self):
            evil_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"private endpoint reached")
        def log_message(self, *args):
            pass
    evil = ThreadingHTTPServer(("127.0.0.1", 0), Evil)
    evil.daemon_threads = True
    evil_url = f"http://127.0.0.1:{evil.server_port}"
    class Allowed(BaseHTTPRequestHandler):
        def do_POST(self):
            evil_hits.append("unauthorized-write:" + self.path)
            self.send_response(204)
            self.end_headers()

        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", evil_url + "/private")
                self.end_headers()
                return
            if self.path == "/download":
                self.send_response(200)
                self.send_header("Content-Disposition", "attachment; filename=unsafe.txt")
                self.end_headers()
                self.wfile.write(b"download must not become host tool artifact")
                return
            cookie = self.headers.get("Cookie", "NONE")
            body = (
                "<!DOCTYPE html><html><body><h1>UNTRUSTED PAGE TEXT</h1>"
                f"<p id='cookie'>cookie: {cookie}</p>"
                "<input id='message'/>"
                "<button id='send' onclick=\"document.getElementById('result').textContent="
                "'submitted ' + document.getElementById('message').value\">Send</button>"
                "<button id='setcookie' onclick=\"document.cookie='session_private=alpha; SameSite=Lax'\">Cookie</button>"
                f"<a id='popup' href='{evil_url}/private' target='_blank'>untrusted popup</a>"
                "<a id='download' href='/download'>download</a>"
                "<div id='result'></div>"
                f"<script>fetch('{evil_url}/collect').catch(()=>{{}})</script>"
                "<script>fetch('/write', {method:'POST', body:'unapproved'}).catch(()=>{})</script>"
                "</body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    allowed = ThreadingHTTPServer(("127.0.0.1", 0), Allowed)
    allowed.daemon_threads = True
    threads = [Thread(target=server.serve_forever, daemon=True) for server in (allowed, evil)]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{allowed.server_port}", evil_hits
    finally:
        for server in (allowed, evil):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3)


def _browser(url):
    return BrowserSession(BrowserURLPolicy(url, allow_loopback=True))


def test_real_chromium_navigation_fill_click_and_subresource_denial(sites):
    origin, evil_hits = sites
    session = _browser(origin)
    try:
        landing = session.call("navigate", {"url": origin + "/"})
        assert "UNTRUSTED PAGE TEXT" in landing["text"]
        assert landing["url"] == origin + "/"
        session.call("fill", {"selector": "#message", "value": "hello"})
        result = session.call("click", {"selector": "#send"})
        assert "submitted hello" in result["text"]
        assert "submitted hello" in session.call("observe", {})["text"]
        assert evil_hits == [], "browser read/navigation caused unauthorized cross-origin or POST request"
    finally:
        session.close()


def test_real_chromium_blocked_redirect_and_independent_cookies(sites):
    origin, evil_hits = sites
    first, second = _browser(origin), _browser(origin)
    try:
        first.call("navigate", {"url": origin + "/"})
        first.call("click", {"selector": "#setcookie"})
        first_cookie = first.call("navigate", {"url": origin + "/"})["text"]
        assert "session_private=alpha" in first_cookie
        other = second.call("navigate", {"url": origin + "/"})["text"]
        assert "cookie: NONE" in other
        with pytest.raises(Exception):
            first.call("navigate", {"url": origin + "/redirect"})
        assert "/private" not in evil_hits
    finally:
        first.close()
        second.close()


def test_real_popup_cannot_open_second_unmanaged_tab(sites):
    origin, evil_hits = sites
    session = _browser(origin)
    try:
        session.call("navigate", {"url": origin + "/"})
        try:
            session.call("click", {"selector": "#popup"})
        except Exception:
            # Closing an unowned popup is allowed to abort the click itself.
            pass
        assert "/private" not in evil_hits, "popup escaped the origin fence"
        assert "UNTRUSTED PAGE TEXT" in session.call("observe", {})["text"]
    finally:
        session.close()
