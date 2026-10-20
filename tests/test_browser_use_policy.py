"""Browser capability is only bound by trusted configuration and explicit UI grants."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import pytest

from core.browser_url_policy import BrowserURLPolicy
from core.runtime.cancellation import scoped_tool_cancellation
from tools.builtin.browser_use import BrowserUseTool


class FakeSession:
    def __init__(self):
        self.calls = []
        self.closed = False

    def call(self, action, opts, cancel_event=None):
        self.calls.append((action, dict(opts)))
        return {"url": "http://127.0.0.1:8179/", "title": "test", "text": "untrusted page"}

    def close(self):
        self.closed = True


def host(*, enabled=True, sandbox="local_trusted", hosted=None, interactive=True,
         broker=None, origins="http://127.0.0.1:8179"):
    config = SimpleNamespace(
        browser_enabled=enabled, browser_allowed_origins=origins,
        browser_allow_loopback=True,
    )
    return SimpleNamespace(
        config=config, _sandbox_mode=sandbox,
        _hosted_tool_authority=hosted,
        _permission_gate=SimpleNamespace(_broker=broker),
        interactive=interactive,
    )


def test_disabled_and_hosted_modes_cannot_register(tmp_path):
    for agent in (host(enabled=False), host(sandbox="docker"),
                  host(sandbox="boxlite"), host(sandbox="e2b"), host(hosted=object())):
        with pytest.raises(PermissionError):
            BrowserUseTool(project_root=tmp_path, code_agent=agent, session=FakeSession())


def test_read_and_navigation_are_origin_scoped(tmp_path):
    fake = FakeSession()
    tool = BrowserUseTool(project_root=tmp_path, code_agent=host(), session=fake)
    assert json.loads(tool.run({"action": "navigate", "url": "http://127.0.0.1:8179/"}))["status"] == "success"
    assert json.loads(tool.run({"action": "observe"}))["status"] == "success"
    assert len(fake.calls) == 2
    blocked = json.loads(tool.run({"action": "navigate", "url": "http://127.0.0.1:8180/"}))
    assert blocked["error"]["code"] == "ACCESS_DENIED"
    assert len(fake.calls) == 2
    tool.close()
    assert fake.closed


def test_click_and_fill_require_unconditionally_fresh_grant(tmp_path):
    fake = FakeSession()
    requests = []
    def approve(detail, name, action):
        requests.append((detail, name, action))
        return "granted"
    agent = host(broker=None)
    tool = BrowserUseTool(project_root=tmp_path, code_agent=agent, session=fake)
    denied = json.loads(tool.run({"action": "click", "selector": "button"}))
    assert denied["status"] == "error" and not fake.calls
    agent._permission_gate._broker = approve
    for action in ("click", "fill"):
        payload = {"action": action, "selector": "#button"}
        if action == "fill":
            payload["value"] = "secret form content"
        assert json.loads(tool.run(payload))["status"] == "success"
    assert len(requests) == 2
    assert requests[0][1:] == ("BrowserUse", "browser-click")
    assert "secret form content" in requests[1][0]
    agent.interactive = False
    assert json.loads(tool.run({"action": "click", "selector": "#button"}))["status"] == "error"
    assert len(fake.calls) == 2


def test_cancelled_tool_cannot_execute(tmp_path):
    fake = FakeSession()
    tool = BrowserUseTool(project_root=tmp_path, code_agent=host(), session=fake)
    flag = Event()
    flag.set()
    class CancelledFake(FakeSession):
        def call(self, action, opts, cancel_event=None):
            if cancel_event.is_set():
                raise InterruptedError()
            return super().call(action, opts, cancel_event)
    tool._session = CancelledFake()
    with scoped_tool_cancellation(flag):
        result = json.loads(tool.run({"action": "observe"}))
    assert result["error"]["code"] == "INTERRUPTED"


def test_auto_bootstrap_does_not_offer_host_browser(tmp_path):
    from core.tool_bootstrap import ToolBootstrap
    from tools.registry import ToolRegistry
    registry = ToolRegistry()
    ToolBootstrap(registry=registry, project_root=str(tmp_path)).discover_and_register()
    assert "BrowserUse" not in registry.list_tools()
