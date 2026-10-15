"""Tenant-scoped hosted tool fence: deny before dispatch, not just in prompts."""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from core.memory.contracts import MemoryScope
from core.runtime.tool_authorization import HostedToolAuthority, trusted_hosted_tool_authority
from desktop.service.hosted_identity import HostedIdentity
from tools.base import Tool, ToolParameter
from tools.registry import ToolRegistry


class Probe(Tool):
    strict_input_contract = True

    def __init__(self, name):
        super().__init__(name, "Isolated probe")
        self.calls = 0

    def get_parameters(self):
        return [ToolParameter(name="value", type="string", description="Value")]

    def run(self, parameters):
        self.calls += 1
        return json.dumps({"status": "success", "data": {}})


class MembershipStore:
    def __init__(self, scopes):
        self.scopes = set(scopes)
        self.fail = False
        self.seen = []

    def authorize_membership(self, scope):
        self.seen.append(scope)
        if self.fail:
            raise ConnectionError("private membership DB unavailable")
        return (scope.tenant_id, scope.user_id, scope.project_id) in self.scopes


def make_registry(scope, store):
    registry = ToolRegistry()
    shell = Probe("Bash")
    host = Probe("Read")
    registry.register_tool(shell, owner="builtin")
    registry.register_tool(host, owner="builtin")
    registry.restrict_to_tools({"Bash"})
    authority = trusted_hosted_tool_authority(HostedIdentity(scope), store)
    registry.bind_hosted_authority(authority)
    return registry, shell, host


def result(registry, name="Bash", snapshot=None):
    return json.loads(registry.execute_tool(
        name, {"value": "x"}, snapshot=snapshot,
    ))


def test_hosted_authority_rejects_unsigned_input_and_unsafe_allowlists():
    scope = MemoryScope("t1", "u1", "p1")
    store = MembershipStore({("t1", "u1", "p1")})
    with pytest.raises(TypeError):
        trusted_hosted_tool_authority({"tenant_id": "t1"}, store)
    with pytest.raises(TypeError):
        trusted_hosted_tool_authority(HostedIdentity(scope), object())
    with pytest.raises(ValueError, match="isolated Bash"):
        HostedToolAuthority(scope, store.authorize_membership, frozenset({"Bash", "Read"}))
    registry = ToolRegistry()
    with pytest.raises(PermissionError, match="isolated"):
        registry.bind_hosted_authority(
            trusted_hosted_tool_authority(HostedIdentity(scope), store),
        )


def test_hosted_identity_is_bound_once_and_cannot_be_swapped():
    scope = MemoryScope("t1", "u1", "p1")
    other = MemoryScope("t2", "u1", "p1")
    store = MembershipStore({("t1", "u1", "p1"), ("t2", "u1", "p1")})
    registry, _, _ = make_registry(scope, store)
    assert registry.get_openai_tools()[0]["function"]["name"] == "Bash"
    with pytest.raises(PermissionError, match="already bound"):
        registry.bind_hosted_authority(
            trusted_hosted_tool_authority(HostedIdentity(other), store),
        )
    assert registry._hosted_authority.scope == scope


def test_cross_tenant_same_user_and_membership_revocation_fail_closed():
    alice = MemoryScope("t1", "same-user", "p1")
    bob = MemoryScope("t2", "same-user", "p1")
    store = MembershipStore({("t1", "same-user", "p1")})
    first, allowed, host = make_registry(alice, store)
    second, denied, _ = make_registry(bob, store)
    assert result(first)["status"] == "success"
    assert result(second)["error"]["code"] == "PERMISSION_DENIED"
    assert second.get_tool("Bash") is None
    assert second.get_openai_tools() == []
    assert second.capture_tool_step().model_schemas() == []
    assert result(first, "Read")["error"]["code"] == "PERMISSION_DENIED"
    assert host.calls == denied.calls == 0
    assert allowed.calls == 1
    assert set(store.seen) == {alice, bob}

    captured = first.capture_tool_step()
    assert captured.model_schemas()
    store.scopes.clear()
    assert first.get_openai_tools() == []
    assert first.get_all_tools() == []
    assert result(first, snapshot=captured)["error"]["code"] == "PERMISSION_DENIED"
    assert allowed.calls == 1

    store.fail = True
    assert result(first)["error"]["code"] == "PERMISSION_DENIED"
    assert first.capture_tool_step().model_schemas() == []


def test_subagent_registry_inherits_non_optional_authority():
    scope = MemoryScope("t1", "u1", "p1")
    store = MembershipStore({("t1", "u1", "p1")})
    parent, _, _ = make_registry(scope, store)
    child = ToolRegistry()
    child_probe = Probe("Bash")
    child.register_tool(child_probe)
    child.inherit_hosted_authority(parent)
    assert child.get_openai_tools()[0]["function"]["name"] == "Bash"
    store.scopes.clear()
    assert child.get_openai_tools() == []
    assert result(child)["error"]["code"] == "PERMISSION_DENIED"
    assert child_probe.calls == 0


def test_membership_isolation_on_parallel_worker_threads():
    a = MemoryScope("t1", "u1", "p1")
    b = MemoryScope("t2", "u1", "p1")
    store = MembershipStore({("t1", "u1", "p1")})
    first, probe, _ = make_registry(a, store)
    second, other, _ = make_registry(b, store)
    jobs = [(first if i % 2 == 0 else second) for i in range(24)]
    with ThreadPoolExecutor(max_workers=6) as workers:
        statuses = list(workers.map(lambda reg: result(reg)["status"], jobs))
    assert statuses == ["success" if i % 2 == 0 else "error" for i in range(24)]
    assert probe.calls == 12
    assert other.calls == 0


def test_real_isolated_codeagent_binds_hosted_authority(tmp_path, monkeypatch):
    from agents.codeAgent import CodeAgent
    from core.config import Config
    from core.sandbox.docker_provider import DockerCommandProvider
    from core.sandbox.docker_lifecycle import DockerLeaseManager
    from tests.test_e2e_agent import ScriptedMockLLM, make_response

    scope = MemoryScope("t1", "u1", "p1")
    store = MembershipStore({("t1", "u1", "p1")})
    authority = trusted_hosted_tool_authority(HostedIdentity(scope), store)
    with pytest.raises(PermissionError, match="isolated sandbox"):
        CodeAgent(
            name="unsafe-hosted", llm=object(),
            tool_registry=ToolRegistry(), project_root=str(tmp_path),
            config=Config(sandbox_mode="local_trusted"),
            hosted_tool_authority=authority,
        )

    provider = DockerCommandProvider(
        tmp_path, docker_binary="/usr/bin/docker",
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "isolated-ok", ""),
        lease_manager=DockerLeaseManager("/usr/bin/docker", b"x" * 32),
    )
    monkeypatch.setattr("core.sandbox.docker_provider.DockerCommandProvider", lambda *a, **kw: provider)
    agent = CodeAgent(
        name="hosted-worker-test",
        llm=ScriptedMockLLM([make_response(content="ok")]),
        tool_registry=ToolRegistry(), project_root=str(tmp_path),
        config=Config(
            sandbox_mode="docker", llm_streaming=False,
            show_react_steps=False, show_progress=False,
            enable_agent_teams=False,
        ),
        hosted_tool_authority=authority,
    )
    try:
        names = [item["function"]["name"] for item in agent._get_openai_tools_for_current_mode()]
        assert names == ["Bash"]
        registry = agent.tool_registry
        assert registry._hosted_authority.scope == scope
        old_step = agent._model_tool_step_snapshot
        store.scopes.clear()
        assert agent._get_openai_tools_for_current_mode() == []
        denied = json.loads(registry.execute_tool(
            "Bash", {"command": "echo x"}, snapshot=old_step,
        ))
        assert denied["error"]["code"] == "PERMISSION_DENIED"
        assert registry.get_tool("Bash") is None
    finally:
        agent.close()


def test_hosted_constructor_never_bootstraps_host_plugins_hooks_or_mcp(tmp_path, monkeypatch):
    """Only framework-owned sandbox starts; untrusted Python cannot initialize."""
    from agents.codeAgent import CodeAgent
    from core.config import Config
    from core.sandbox.docker_provider import DockerCommandProvider
    from core.sandbox.docker_lifecycle import DockerLeaseManager
    from core.runtime.service_sandbox import SandboxService
    from tests.test_e2e_agent import ScriptedMockLLM, make_response

    scope = MemoryScope("secure-tenant", "u1", "project")
    membership = MembershipStore({("secure-tenant", "u1", "project")})
    authority = trusted_hosted_tool_authority(HostedIdentity(scope), membership)
    def unexpected(*args, **kwargs):
        raise AssertionError("Untrusted host-side adapter must not initialize")

    monkeypatch.setattr("core.plugin_loader.PluginLoader.discover", unexpected)
    monkeypatch.setattr("tools.mcp.loader.register_mcp_servers", unexpected)
    monkeypatch.setattr("core.tool_bootstrap.ToolBootstrap.discover_and_register", unexpected)
    monkeypatch.setattr("core.hook_system.HookManager", unexpected)
    provider = DockerCommandProvider(
        tmp_path, docker_binary="/usr/bin/docker",
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "ok", ""),
        lease_manager=DockerLeaseManager("/usr/bin/docker", b"x" * 32),
    )
    monkeypatch.setattr("core.sandbox.docker_provider.DockerCommandProvider",
                        lambda *args, **kwargs: provider)
    agent = CodeAgent(
        name="hosted-fail-closed",
        llm=ScriptedMockLLM([make_response(content="done")]),
        tool_registry=ToolRegistry(),
        project_root=str(tmp_path),
        config=Config(sandbox_mode="docker", enable_agent_teams=False,
                      llm_streaming=False, show_react_steps=False,
                      show_progress=False),
        hosted_tool_authority=authority,
    )
    try:
        assert agent._runtime_services.started == ("sandbox",)
        assert {f.name for f in agent._features} == {
            "delegate_mode", "budget", "plan_mode",
        }
        assert [t["function"]["name"] for t in agent._get_openai_tools_for_current_mode()] == ["Bash"]
        assert getattr(agent, "_session_manager", None) is None
        with pytest.raises(PermissionError, match="authenticated fenced worker"):
            agent.run("execute a tenant instruction")
    finally:
        agent.close()
