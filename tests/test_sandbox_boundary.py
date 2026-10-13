"""Container sandbox tests: deny host fallbacks, seal registry, validate Docker flags.

Fake runner covers argument construction without requiring Docker installation.
Real containment needs a separate machine with the preloaded image.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import Config
from core.sandbox.docker_provider import DockerCommandProvider, SandboxUnavailable
from core.sandbox.command_tool import SandboxBashTool
from tools.registry import ToolRegistry
from tools.builtin.read_file import ReadTool


def _provider(workspace: Path, runner=None):
    return DockerCommandProvider(
        workspace, docker_binary="/usr/bin/docker", runner=runner
    )


def test_docker_flags_no_network_no_privilege_no_host_environment(tmp_path, monkeypatch):
    monkeypatch.setattr("core.sandbox.docker_provider.os.getuid", lambda: 0)
    monkeypatch.setattr("core.sandbox.docker_provider.os.getgid", lambda: 0)
    p = _provider(tmp_path)
    argv, name = p.command_argv("python -c 'print(123)'")
    assert name.startswith("wodeagent-")
    assert argv[:2] == ["/usr/bin/docker", "run"]
    for value in ("--rm", "--pull=never", "--read-only", "--cap-drop=ALL", "--network=none", "--pids-limit", "--memory", "--cpus"):
        assert value in argv
    assert argv[argv.index("--user") + 1] == "65534:65534"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--env") + 1] == "HOME=/tmp"
    assert argv[argv.index("--workdir") + 1] == "/workspace"
    assert not any("DOCKER_HOST=" in x or "API_KEY=" in x for x in argv)
    assert not any("docker.sock" in x for x in argv)
    assert argv[-2:] == ["-lc", "python -c 'print(123)'"]


def test_only_in_root_workdirs_accepted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sub = root / "sub"
    sub.mkdir()
    p = _provider(root)
    argv, _ = p.command_argv("pwd", directory="sub")
    assert argv[argv.index("--workdir") + 1] == "/workspace/sub"
    with pytest.raises(PermissionError):
        p.command_argv("pwd", directory="..")
    with pytest.raises(PermissionError):
        p.command_argv("pwd", directory=str(tmp_path))
    link = root / "escape"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PermissionError):
        p.command_argv("pwd", directory="escape")


def test_docker_unavailable_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("core.sandbox.docker_provider.shutil.which", lambda _: None)
    with pytest.raises(SandboxUnavailable):
        DockerCommandProvider(tmp_path)
    events = []
    def fail_runner(*args, **kwargs):
        events.append(args)
        raise OSError("docker daemon missing")
    p = _provider(tmp_path, fail_runner)
    with pytest.raises(SandboxUnavailable):
        p.execute("echo hello")
    assert len(events) == 1
    assert events[0][0][0:2] == ["/usr/bin/docker", "run"]


def test_docker_timeout_requests_force_removal(tmp_path):
    calls = []
    def runner(argv, **kw):
        calls.append(list(argv))
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, timeout=1, output=b"partial")
        return subprocess.CompletedProcess(argv, 0, "", "")
    p = _provider(tmp_path, runner)
    response = p.execute("sleep 100", timeout_seconds=1)
    assert response.timed_out and response.stdout == "partial"
    assert calls[1][1:3] == ["rm", "--force"]
    assert calls[1][3] == calls[0][calls[0].index("--name") + 1]


def test_isolated_tool_provides_protocol_and_never_runs_host_shell(tmp_path):
    commands = []
    def runner(argv, **kw):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "container-ok\n", "")
    tool = SandboxBashTool(_provider(tmp_path, runner))
    result = json.loads(tool.run({"command": "echo container-ok"}))
    assert result["status"] == "success"
    assert result["data"]["sandbox"] == "docker"
    assert result["data"]["stdout"] == "container-ok\n"
    assert commands and commands[0][:2] == ["/usr/bin/docker", "run"]


def test_registry_seals_preexisting_and_later_registered_tools(tmp_path):
    registry = ToolRegistry()
    read = ReadTool(project_root=tmp_path)
    registry.register_tool(read)
    calls = []
    registry.register_function("ShellHost", "run host shell", lambda _: calls.append(1))
    shell = SandboxBashTool(_provider(
        tmp_path, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "ok", "")
    ))
    registry.register_tool(shell)
    registry.restrict_to_tools({"Bash"})
    assert registry.list_tools() == ["Bash"]
    assert registry.get_tool("Read") is None
    assert registry.get_function("ShellHost") is None
    assert [x["function"]["name"] for x in registry.get_openai_tools()] == ["Bash"]
    for name in ("Read", "ShellHost"):
        response = json.loads(registry.execute_tool(name, {"path": "/etc/passwd"}))
        assert response["status"] == "error"
        assert response["error"]["code"] == "ACCESS_DENIED" or response["error"]["code"] == "access_denied"
    assert calls == []
    registry.register_function("LateMCP", "late provider", lambda _: calls.append(2))
    result = json.loads(registry.execute_tool("LateMCP", {"input": "x"}))
    assert result["status"] == "error"
    assert calls == []


def test_config_sandbox_mode_defaults_to_local_trusted():
    assert Config().sandbox_mode == "local_trusted"


def test_bad_resource_config_refuses_sandbox(tmp_path):
    with pytest.raises(SandboxUnavailable):
        DockerCommandProvider(tmp_path, image="python,cap-add=SYS_ADMIN", docker_binary="docker")
    with pytest.raises(SandboxUnavailable):
        DockerCommandProvider(tmp_path, memory="invalid", docker_binary="docker")
    with pytest.raises(SandboxUnavailable):
        DockerCommandProvider(tmp_path, cpus=-5, docker_binary="docker")


def test_codeagent_installs_container_tool_and_registry_guard(tmp_path, monkeypatch):
    from agents.codeAgent import CodeAgent
    from tests.test_e2e_agent import ScriptedMockLLM, make_response

    p = _provider(tmp_path, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "ok", ""))
    monkeypatch.setattr("core.sandbox.docker_provider.DockerCommandProvider", lambda *a, **k: p)
    agent = CodeAgent(
        name="docker-agent",
        llm=ScriptedMockLLM([make_response(content="done")]),
        tool_registry=ToolRegistry(),
        project_root=str(tmp_path),
        config=Config(
            sandbox_mode="docker", llm_streaming=False,
            show_react_steps=False, show_progress=False,
            enable_agent_teams=False,
        ),
    )
    assert agent.tool_registry.list_tools() == ["Bash"]
    assert isinstance(agent.tool_registry.get_tool("Bash"), SandboxBashTool)
    assert agent.tool_registry.get_tool("Read") is None
    assert json.loads(agent.tool_registry.execute_tool("Read", {"path": "/etc/shadow"}))["status"] == "error"
    with pytest.raises(RuntimeError):
        agent._rebind_project_root(str(tmp_path / "next"))
    agent.close()


def test_docker_mode_refuses_multi_agent_fallback(tmp_path):
    from agents.codeAgent import CodeAgent
    with pytest.raises(ValueError, match="AgentTeams"):
        CodeAgent(
            name="bad", llm=object(), tool_registry=ToolRegistry(),
            project_root=str(tmp_path),
            config=Config(sandbox_mode="docker", enable_agent_teams=True),
        )
