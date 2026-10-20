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


def test_agent_binds_browser_only_in_trusted_local_mode(tmp_path):
    from agents.codeAgent import CodeAgent
    from core.config import Config
    from tests.test_e2e_agent import ScriptedMockLLM
    from tools.registry import ToolRegistry

    def build(config):
        return CodeAgent(
            name="browser-test", llm=ScriptedMockLLM([]),
            tool_registry=ToolRegistry(), project_root=str(tmp_path),
            config=config,
        )

    config = Config(
        browser_enabled=True,
        browser_allowed_origins="http://127.0.0.1:8179",
        browser_allow_loopback=True,
        show_react_steps=False,
    )
    agent = build(config)
    try:
        assert "BrowserUse" in agent.tool_registry.list_tools()
        assert agent.tool_registry.get_tool("BrowserUse") is agent._browser_tool
        assert agent._browser_tool._session._thread is None  # no eager browser
        assert agent.tool_registry.derive_worker_registry().get_tool("BrowserUse") is None
    finally:
        agent.close()
    assert agent._browser_tool is None
    assert "BrowserUse" not in agent.tool_registry.list_tools()

    # A shared registry cannot silently lend an active browser to another Agent.
    shared_registry = ToolRegistry()
    first = CodeAgent(
        name="first", llm=ScriptedMockLLM([]),
        tool_registry=shared_registry, project_root=str(tmp_path), config=config,
    )
    try:
        with pytest.raises(PermissionError, match="already owned"):
            CodeAgent(
                name="second", llm=ScriptedMockLLM([]),
                tool_registry=shared_registry, project_root=str(tmp_path),
                config=Config(browser_enabled=False),
            )
    finally:
        first.close()
    assert "BrowserUse" not in shared_registry.list_tools()

    with pytest.raises(PermissionError, match="trusted local"):
        build(config.model_copy(update={"sandbox_mode": "docker"}))
    with pytest.raises(PermissionError, match="trusted local"):
        build(config.model_copy(update={"require_isolated_execution": True}))
