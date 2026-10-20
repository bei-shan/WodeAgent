"""Execution-layer security regressions: credentials, process env and mounts."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.config import Config
from core.runtime.subprocess_environment import safe_child_env
from core.sandbox.contracts import SandboxUnavailable
from core.sandbox.docker_provider import DockerCommandProvider
from core.sandbox.boxlite_provider import BoxliteCommandProvider
from core.sandbox.workspace_secrets import assert_no_mounted_credentials
from tools.builtin.bash import BashTool
from tools.mcp.client import MCPClient, MCPClientConfig


def test_child_environment_excludes_ambient_secrets_and_runtime_hooks(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "DO_NOT_PASS_ME")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "DO_NOT_PASS_ME")
    monkeypatch.setenv("GITHUB_TOKEN", "DO_NOT_PASS_ME")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/startup")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/untrusted/startup")
    monkeypatch.setenv("LD_PRELOAD", "/untrusted/inject.so")
    monkeypatch.setenv("BASH_ENV", "/untrusted/startup")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("HTTP_PROXY", "http://user:password@proxy")
    clean = safe_child_env(home=str(tmp_path))
    for name in (
        "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
        "PYTHONPATH", "NODE_OPTIONS", "LD_PRELOAD", "BASH_ENV",
        "GIT_CONFIG_COUNT", "HTTP_PROXY",
    ):
        assert name not in clean
    assert clean["HOME"] == str(tmp_path)
    assert "PATH" in clean
    assert safe_child_env({"MCP_EXPLICIT_TOKEN": "trusted-grant"})["MCP_EXPLICIT_TOKEN"] == "trusted-grant"


def test_child_environment_rejects_invalid_overrides():
    with pytest.raises(ValueError):
        safe_child_env({"BAD=KEY": "value"})
    with pytest.raises(ValueError):
        safe_child_env({"GOOD": "evil\x00value"})
    with pytest.raises(TypeError):
        safe_child_env(["not", "a", "mapping"])


def test_local_bash_subprocess_receives_temporary_home_and_no_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-inherit")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-inherit")
    seen = []

    def fake_run(args, **kwargs):
        seen.append(dict(kwargs["env"]))
        assert os.path.isdir(kwargs["env"]["HOME"])
        return SimpleNamespace(stdout="ok", stderr="", returncode=0,
                               timed_out=False, interrupted=False)

    monkeypatch.setattr("tools.builtin.bash.run_process", fake_run)
    result = BashTool(project_root=tmp_path).run({"command": "echo hello"})
    assert '"success"' in result
    assert len(seen) == 1
    assert "OPENAI_API_KEY" not in seen[0] and "AWS_SECRET_ACCESS_KEY" not in seen[0]
    assert seen[0]["MYCODEAGENT"] == "1"
    assert not Path(seen[0]["HOME"]).exists()


def test_mcp_stdio_never_inherits_agent_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stolen")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "stolen")

    async def fake_connect():
        return (object(), object(), None)

    connection = AsyncMock()
    connection.__aenter__.side_effect = fake_connect
    session = AsyncMock()
    with (patch("tools.mcp.client.stdio_client", return_value=connection) as mock_stdio,
          patch("tools.mcp.client.ClientSession", return_value=session)):
        client = MCPClient(MCPClientConfig(
            transport="stdio", command="echo",
            env={"EXPLICIT_MCP_TOKEN": "approved"},
        ))
        client.connect_sync()
        try:
            supplied = mock_stdio.call_args.args[0].env
            assert supplied["EXPLICIT_MCP_TOKEN"] == "approved"
            assert "OPENAI_API_KEY" not in supplied
            assert "AWS_SECRET_ACCESS_KEY" not in supplied
        finally:
            client.close_sync()


def test_docker_rejects_credential_files_before_mount(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env.example").write_text("EXAMPLE=ok")
    provider = DockerCommandProvider(root, docker_binary="/usr/bin/docker",
                                     runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    assert "--network=none" in provider.command_argv("echo ok")[0]
    secret = root / ".env"
    secret.write_text("MY_SECRET=private")
    with pytest.raises(SandboxUnavailable, match="credential file"):
        provider.command_argv("echo should-not-run")
    with pytest.raises(SandboxUnavailable):
        DockerCommandProvider(root, docker_binary="/usr/bin/docker", runner=lambda *a, **k: None)


def test_boxlite_rejects_credentials_before_vm_creation(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    p = BoxliteCommandProvider(root, box_factory=lambda **kwargs: kwargs,
                               network_factory=dict)
    (root / ".ssh").mkdir()
    with pytest.raises(SandboxUnavailable, match="credential directory"):
        p._make_box()
    with pytest.raises(SandboxUnavailable):
        BoxliteCommandProvider(root, box_factory=lambda **kwargs: kwargs,
                               network_factory=dict)


def test_nested_credentials_and_public_templates(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / ".env.example").write_text("TOKEN=not-a-secret")
    (tmp_path / "src" / ".env.template").write_text("TOKEN=example")
    assert_no_mounted_credentials(tmp_path)
    (tmp_path / "src" / ".env.production").write_text("TOKEN=real")
    with pytest.raises(SandboxUnavailable, match="credential file"):
        assert_no_mounted_credentials(tmp_path)


def test_strict_isolation_policy_denies_host_agent(tmp_path):
    from agents.codeAgent import CodeAgent
    from tools.registry import ToolRegistry
    from tests.test_e2e_agent import ScriptedMockLLM, make_response

    with pytest.raises(PermissionError, match="requires isolated execution"):
        CodeAgent(
            name="strict-agent",
            llm=ScriptedMockLLM([make_response(content="done")]),
            tool_registry=ToolRegistry(),
            project_root=str(tmp_path),
            config=Config(sandbox_mode="local_trusted", require_isolated_execution=True),
        )
