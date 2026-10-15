"""Provider-free E2B remote VM contract, ownership and failure injection."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from core.sandbox.contracts import SandboxUnavailable
from core.sandbox.e2b_provider import E2BCapacity, E2BCommandProvider


class FakeCommands:
    def __init__(self, vm):
        self.vm = vm
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.vm.running.set()
        if self.vm.block:
            self.vm.gone.wait(timeout=5)
        if self.vm.failure is not None:
            raise self.vm.failure
        return SimpleNamespace(stdout="remote-output", stderr="", exit_code=0)


class FakeVM:
    def __init__(self):
        self.commands = FakeCommands(self)
        self.running = threading.Event()
        self.gone = threading.Event()
        self.killed = 0
        self.block = False
        self.kill_error = False
        self.failure = None

    def kill(self):
        self.killed += 1
        if self.kill_error:
            raise OSError("remote API unavailable")
        self.gone.set()
        return True


class Factory:
    def __init__(self):
        self.calls = []
        self.vms = []
        self.error = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        vm = FakeVM()
        self.vms.append(vm)
        return vm


def provider(root, factory=None, capacity=None, **kwargs):
    return E2BCommandProvider(
        root, api_key="fake-e2b-key", sdk_factory=factory or Factory(),
        capacity=capacity or E2BCapacity(2),
        single_gateway_ack=True, **kwargs,
    )


def test_credentials_and_explicit_single_gateway_consent_required(tmp_path, monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    with pytest.raises(SandboxUnavailable, match="acknowledgement"):
        E2BCommandProvider(tmp_path, api_key="secret", sdk_factory=Factory())
    with pytest.raises(SandboxUnavailable, match="credential"):
        E2BCommandProvider(
            tmp_path, single_gateway_ack=True, sdk_factory=Factory(),
        )


def test_lazy_remote_vm_creation_and_no_workspace_mount_or_secret_metadata(tmp_path):
    factory = Factory()
    cap = E2BCapacity(1)
    p = provider(tmp_path, factory, cap)
    assert not factory.calls and cap.used == 0
    result = p.execute("printf ok")
    assert result.exit_code == 0 and result.stdout == "remote-output"
    assert cap.used == 1 and len(factory.calls) == 1
    args = factory.calls[0]
    assert args["template"] == "code-interpreter-v1"
    assert args["timeout"] == 300
    assert args["allow_internet_access"] is False
    assert "fake-e2b-key" not in str(args["metadata"])
    assert "volumes" not in args and "mounts" not in args
    assert tmp_path.as_posix() not in str(args["metadata"])
    assert factory.vms[0].commands.calls == [
        ("printf ok", {"cwd": "/home/user", "timeout": 120}),
    ]
    assert p.execute("echo second").exit_code == 0
    assert len(factory.calls) == 1
    p.close()
    p.close()
    assert factory.vms[0].killed == 1
    assert cap.used == 0
    with pytest.raises(SandboxUnavailable, match="closed"):
        p.execute("echo fail")


def test_no_host_dir_mapping_and_traversal_disallowed_before_remote_create(tmp_path):
    factory = Factory()
    p = provider(tmp_path, factory)
    for directory in ("../secret", "/workspace", "/", str(tmp_path)):
        with pytest.raises(PermissionError):
            p.execute("ls", directory=directory)
    assert not factory.calls and p.capacity_used == 0


def test_capacity_hard_limit_blocks_other_session_until_confirmed_kill(tmp_path):
    cap = E2BCapacity(1)
    first, second = Factory(), Factory()
    a = provider(tmp_path, first, cap)
    b = provider(tmp_path, second, cap)
    assert a.execute("echo A").exit_code == 0
    with pytest.raises(SandboxUnavailable, match="capacity"):
        b.execute("echo B")
    assert not second.calls
    a.close()
    assert cap.used == 0
    assert b.execute("echo B").exit_code == 0
    b.close()


def test_unknown_create_keeps_capacity_reserved_no_blind_retry(tmp_path):
    cap = E2BCapacity(1)
    first, second = Factory(), Factory()
    first.error = RuntimeError("opaque timeout")
    a = provider(tmp_path, first, cap)
    with pytest.raises(SandboxUnavailable, match="creation"):
        a.execute("do-not-retry")
    assert cap.used == 1
    assert len(first.calls) == 1
    with pytest.raises(SandboxUnavailable):
        a.execute("do-not-retry")
    b = provider(tmp_path, second, cap)
    with pytest.raises(SandboxUnavailable, match="capacity"):
        b.execute("more")
    assert not second.calls
    a.close()
    assert cap.used == 1


def test_unknown_command_result_destroys_vm_without_replay(tmp_path):
    factory = Factory()
    cap = E2BCapacity(1)
    p = provider(tmp_path, factory, cap)
    p.execute("warm")
    factory.vms[0].failure = OSError("network lost")
    with pytest.raises(SandboxUnavailable, match="unknown"):
        p.execute("dangerous mutation")
    assert factory.vms[0].killed == 1
    assert cap.used == 0
    assert len(factory.vms[0].commands.calls) == 2
    with pytest.raises(SandboxUnavailable):
        p.execute("dangerous mutation")


def test_cancel_kills_remote_and_releases_confirmed_capacity(tmp_path):
    factory = Factory()
    cap = E2BCapacity(1)
    p = provider(tmp_path, factory, cap)
    assert p.execute("initial").exit_code == 0
    factory.vms[0].block = True
    cancelled = threading.Event()
    results = []

    t = threading.Thread(target=lambda: results.append(p.execute(
        "sleep 600", timeout_seconds=30, cancel_event=cancelled,
    )))
    t.start()
    assert factory.vms[0].running.wait(timeout=2)
    time.sleep(0.1)
    cancelled.set()
    t.join(timeout=4)
    assert not t.is_alive()
    assert results and results[0].interrupted
    assert factory.vms[0].killed == 1
    assert cap.used == 0
    with pytest.raises(SandboxUnavailable):
        p.execute("no replay")


def test_failed_kill_retains_capacity_until_operator_reconcile(tmp_path):
    factory = Factory()
    cap = E2BCapacity(1)
    p = provider(tmp_path, factory, cap)
    p.execute("start")
    factory.vms[0].kill_error = True
    p.close()
    assert cap.used == 1
    assert factory.vms[0].killed == 1
    with pytest.raises(SandboxUnavailable):
        provider(tmp_path, Factory(), cap).execute("blocked")


def test_nonzero_remote_command_error_stays_a_command_result(tmp_path):
    factory = Factory()
    p = provider(tmp_path, factory)
    p.execute("start")
    class CommandExit(Exception):
        stdout = ""
        stderr = "not found"
        exit_code = 127
    factory.vms[0].failure = CommandExit()
    result = p.execute("missing-command")
    assert result.exit_code == 127 and result.stderr == "not found"
    p.close()


def test_agent_mode_seals_host_tools_and_closes_e2b(tmp_path, monkeypatch):
    from agents.codeAgent import CodeAgent
    from core.config import Config
    from tests.test_e2e_agent import ScriptedMockLLM, make_response
    from tools.registry import ToolRegistry

    factory = Factory()
    p = provider(tmp_path, factory)
    monkeypatch.setattr("core.sandbox.e2b_provider.E2BCommandProvider", lambda *a, **k: p)
    agent = CodeAgent(
        "e2b-agent", ScriptedMockLLM([make_response(content="done")]),
        ToolRegistry(), str(tmp_path),
        config=Config(sandbox_mode="e2b", llm_streaming=False,
                      show_react_steps=False, show_progress=False,
                      enable_agent_teams=False),
    )
    try:
        assert agent.tool_registry.list_tools() == ["Bash"]
        assert "REMOTE E2B" in agent.tool_registry.get_tool("Bash").usage_notes
        assert agent.tool_registry.get_tool("Read") is None
        assert p.execute("ok").exit_code == 0
    finally:
        agent.close()
    assert factory.vms[0].killed == 1
