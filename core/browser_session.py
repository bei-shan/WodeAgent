"""Session-owned Playwright browser controller; never shared across Agents.

Playwright's sync API is thread-affine. A dedicated owner thread serializes
all browser operations, owns the transient profile and closes the browser at
Agent shutdown. No arbitrary script evaluation, file upload, downloads or
host filesystem exposure is provided by this API.
"""
from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any

from core.browser_url_policy import BrowserAccessDenied, BrowserURLPolicy


class BrowserUnavailable(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, policy: BrowserURLPolicy):
        self.policy = policy
        self._queue: Queue[Any] = Queue(maxsize=2)
        self._ready = Event()
        self._lock = Lock()
        self._serial = Lock()
        self._closed = False
        self._thread: Thread | None = None

    def _worker(self) -> None:
        browser = context = None
        try:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise BrowserUnavailable(
                    "Playwright is optional: install requirements-browser.txt and chromium"
                ) from exc
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    accept_downloads=False, service_workers="block",
                    permissions=[], ignore_https_errors=False,
                )
                def guard(route):
                    if self.policy.allows(route.request.url):
                        route.continue_()
                    else:
                        route.abort(error_code="blockedbyclient")
                context.route("**/*", guard)
                # No WebSocket exfiltration in this opt-in local browser.
                if not hasattr(context, "route_web_socket"):
                    raise BrowserUnavailable("Playwright requires WebSocket route blocking support")
                context.route_web_socket("**/*", lambda websocket: websocket.close())
                page = context.new_page()
                page.set_default_timeout(4500)
                # Browser popups cannot obtain their own unmanaged session/tab.
                context.on("page", lambda new_page: new_page.close() if new_page != page else None)
                page.on("download", lambda download: download.cancel())
                self._ready.set()
                while True:
                    task = self._queue.get()
                    if task is None:
                        break
                    action, opts, cancelled, answer = task
                    try:
                        if cancelled is not None and cancelled.is_set():
                            raise InterruptedError("Browser action interrupted before execution")
                        result = self._operate(page, action, opts)
                        if cancelled is not None and cancelled.is_set():
                            raise InterruptedError("Browser action interrupted")
                        answer.put((True, result))
                    except BaseException as exc:
                        answer.put((False, exc))
        except BaseException as exc:
            self._ready.set()
            # If startup fails, inform any queued caller rather than deadlock.
            while True:
                try:
                    task = self._queue.get_nowait()
                except Empty:
                    break
                if task is not None:
                    task[3].put((False, exc))
            self._startup_error = exc
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    def _operate(self, page: Any, action: str, opts: dict[str, str]) -> dict[str, str]:
        if action == "navigate":
            self.policy.check(opts["url"])
            page.goto(opts["url"], wait_until="domcontentloaded", timeout=9000)
        elif action == "click":
            if page.url == "about:blank":
                raise BrowserAccessDenied("Navigate to an approved origin first")
            self.policy.check(page.url)
            page.locator(opts["selector"]).first.click(timeout=4500)
        elif action == "fill":
            if page.url == "about:blank":
                raise BrowserAccessDenied("Navigate to an approved origin first")
            self.policy.check(page.url)
            page.locator(opts["selector"]).first.fill(opts["value"], timeout=4500)
        elif action != "observe":
            raise ValueError("Unknown browser action")
        if page.url == "about:blank":
            return {"url": "about:blank", "title": "", "text": ""}
        try:
            self.policy.check(page.url)
        except BrowserAccessDenied:
            page.goto("about:blank")
            raise
        try:
            body = page.locator("body").inner_text(timeout=4500)
        except Exception:
            body = ""
        return {
            "url": page.url,
            "title": page.title()[:512],
            "text": body[:12000],
            "truncated": str(len(body) > 12000).lower(),
        }

    def call(self, action: str, opts: dict[str, str], cancel_event: Event | None = None) -> dict[str, str]:
        with self._serial:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Browser action cancelled")
            with self._lock:
                if self._closed:
                    raise BrowserUnavailable("Browser session has closed")
                if self._thread is None:
                    self._thread = Thread(target=self._worker, name="ridge-browser-session", daemon=True)
                    self._thread.start()
            if not self._ready.wait(timeout=15):
                self.close()
                raise BrowserUnavailable("Browser startup timed out")
            startup_error = getattr(self, "_startup_error", None)
            if startup_error:
                raise BrowserUnavailable("Browser runtime unavailable") from startup_error
            answer: Queue[Any] = Queue(maxsize=1)
            self._queue.put((action, opts, cancel_event, answer), timeout=2)
            while True:
                try:
                    ok, value = answer.get(timeout=0.1)
                except Empty:
                    if cancel_event is not None and cancel_event.is_set():
                        # Cancels queued actions before dispatch. An already
                        # running sync Playwright operation has a finite timeout.
                        self._closed = True
                        raise InterruptedError("Browser action cancelled; session invalidated")
                    if self._thread is None or not self._thread.is_alive():
                        raise BrowserUnavailable("Browser session exited unexpectedly")
                    continue
                if not ok:
                    raise value
                return value

    def close(self) -> None:
        with self._lock:
            self._closed = True
            worker = self._thread
        if worker is None:
            return
        try:
            self._queue.put(None, timeout=1)
        except Exception:
            pass
        if worker is not Thread.current_thread() if False else True:
            worker.join(timeout=15)
        if worker.is_alive():
            raise BrowserUnavailable("Browser owner did not stop within bounded shutdown")
