"""BoxLite is a real opt-in VM backend, never an alias for host Bash."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.sandbox.boxlite_provider import BoxliteCommandProvider
from core.sandbox.command_tool import SandboxBashTool
from core.sandbox.docker_provider import SandboxUnavailable
from core.runtime.cancellation import scoped_tool_cancellation


class FakeBox:
    calls: list[dict] = []
    closed: list[str] = []
    started = threading.Event()

    def __init__(self, **options):
        self.options = options
        self.name = options["name"]
        self.calls.append(options)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed.append(self.name)

    async def exec(self, *argv, **kwargs):
        assert argv[:2] == ("sh", "-lc")
        self.started.set()
        if "sleep" in argv[2]:
            await asyncio.sleep(60)
        return SimpleNamespace(stdout="guest-ok", stderr="", exit_code=0)


def make_provider(workspace, *, box_factory=FakeBox, **kwargs):
    return BoxliteCommandProvider(
        workspace, box_factory=box_factory, network_factory=dict, **kwargs,
    )


def test_boxlite_uses_networkless_vm_not_host_shell_and_preserves_workspace(tmp_path):
    FakeBox.calls.clear()
    FakeBox.closed.clear()
    root = tmp_path / "project"
    root.mkdir()
    (root / "sub").mkdir()
    p = make_provider(root)
    result = p.execute("printf hello", directory="sub", timeout_seconds=3)
    assert result.exit_code == 0 and result.stdout == "guest-ok"
    assert len(FakeBox.closed) == 1
    opts = FakeBox.calls[-1]
    assert opts["network"] == {"mode": "disabled"}
    assert opts["volumes"] == [(str(root), "/workspace", False)]
    assert opts["auto_remove"] is True
    assert opts["image"] == "python:3.12-slim"
    assert opts["name"].startswith("ridgecode-")
    assert opts["memory_mib"] == 1024 and opts["cpus"] == 2


def test_boxlite_rejects_path_traversal_and_outside_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    link = root / "escaped"
    link.symlink_to(tmp_path, target_is_directory=True)
    p = make_provider(root)
    with pytest.raises(PermissionError):
        p.execute("echo fail", directory="../")
    with pytest.raises(PermissionError):
        p.execute("echo fail", directory="escaped")
    with pytest.raises(ValueError):
        p.execute("echo fail", directory="missing")


def test_boxlite_pre_cancelled_never_creates_guest(tmp_path):
    FakeBox.calls.clear()
    event = threading.Event()
    event.set()
    p = make_provider(tmp_path)
    result = p.execute("echo never", cancel_event=event)
    assert result.interrupted and result.exit_code is None
    assert not FakeBox.calls


def test_boxlite_cancel_during_guest_exec_closes_vm_and_returns_protocol(tmp_path):
    FakeBox.started.clear()
    FakeBox.closed.clear()
    event = threading.Event()
    outputs = []
    p = make_provider(tmp_path)
    tool = SandboxBashTool(p)

    def worker():
        with scoped_tool_cancellation(event):
            outputs.append(json.loads(tool.run({
                "command": "sleep 60", "timeout_ms": 5000,
            })))

    thread = threading.Thread(target=worker)
    thread.start()
    assert FakeBox.started.wait(timeout=2)
    event.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert outputs[0]["error"]["code"] == "INTERRUPTED"
    assert FakeBox.closed


def test_boxlite_deadline_produces_timeout_and_closes_vm(tmp_path):
    FakeBox.closed.clear()
    p = make_provider(tmp_path)
    result = p.execute("sleep 60", timeout_seconds=0.15)
    assert result.timed_out and result.exit_code is None
    assert FakeBox.closed


def test_boxlite_vm_failure_is_fail_closed(tmp_path):
    class FailingBox(FakeBox):
        async def __aenter__(self):
            raise OSError("VM provider unavailable")

    p = make_provider(tmp_path, box_factory=FailingBox)
    with pytest.raises(SandboxUnavailable, match="host fallback forbidden"):
        p.execute("echo denied")


@pytest.mark.parametrize("kwargs", [
    {"memory_mib": 0}, {"memory_mib": True}, {"memory_mib": 50000},
    {"cpus": 0}, {"cpus": 1.5}, {"image": "--privileged"},
])
def test_boxlite_bad_limits_refuse_before_vm_creation(tmp_path, kwargs):
    with pytest.raises(SandboxUnavailable):
        make_provider(tmp_path, **kwargs)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), True, 0, -1])
def test_boxlite_invalid_timeout_refused(tmp_path, timeout):
    p = make_provider(tmp_path)
    with pytest.raises(ValueError):
        p.execute("true", timeout_seconds=timeout)


def test_boxlite_sdk_is_explicitly_optional_and_no_docker_fallback(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "boxlite", None)
    with pytest.raises(SandboxUnavailable, match="SDK is not installed"):
        BoxliteCommandProvider(tmp_path)


def test_codeagent_vm_mode_seals_host_tools(tmp_path, monkeypatch):
    from agents.codeAgent import CodeAgent
    from core.config import Config
    from tests.test_e2e_agent import ScriptedMockLLM, make_response
    from tools.registry import ToolRegistry

    provider = make_provider(tmp_path)
    monkeypatch.setattr("core.sandbox.boxlite_provider.BoxliteCommandProvider", lambda *a, **k: provider)
    agent = CodeAgent(
        name="vm-agent",
        llm=ScriptedMockLLM([make_response(content="done")]),
        tool_registry=ToolRegistry(), project_root=str(tmp_path),
        config=Config(sandbox_mode="boxlite", llm_streaming=False,
                      show_react_steps=False, show_progress=False,
                      enable_agent_teams=False),
    )
    try:
        assert agent.tool_registry.list_tools() == ["Bash"]
        assert agent.tool_registry.get_tool("Read") is None
        result = json.loads(agent.tool_registry.execute_tool("Read", {"path": "/etc/passwd"}))
        assert result["status"] == "error"
        output = json.loads(agent.tool_registry.execute_tool("Bash", {"command": "echo good"}))
        assert output["data"]["sandbox"] == "boxlite"
        assert output["status"] == "success"
        with pytest.raises(RuntimeError):
            agent._rebind_project_root(str(tmp_path / "other"))
    finally:
        agent.close()
