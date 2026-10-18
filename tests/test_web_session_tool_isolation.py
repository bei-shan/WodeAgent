"""Default Web factory regression: real agents must own their tool state."""
from __future__ import annotations

import json
from pathlib import Path
import runpy

from fastapi.testclient import TestClient
import pytest

from core.config import Config
from desktop.service.app import create_app
from scripts.agent_eval import ScriptedProvider, answer, tool
from tools.base import Tool
from tools.registry import ToolRegistry


@pytest.fixture
def default_web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "")
    monkeypatch.setenv("RIDGE_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("MCP_CONNECT_MODE", "disabled")
    monkeypatch.setenv("TRACE_ENABLED", "false")
    config = Config(
        agent_interactive=False, llm_streaming=False, trace_enabled=False,
        enable_agent_teams=False, show_progress=False, show_react_steps=False,
    )
    monkeypatch.setattr("core.config.Config.from_env", lambda: config)
    providers = []

    def make_provider():
        marker = f"agent-{len(providers)}"
        provider = ScriptedProvider((
            answer(calls=[tool("Read", {"path": "sentinel.txt"}, 1)]),
            answer(calls=[tool("Write", {"path": "output.txt", "content": marker}, 2)]),
            answer("done"),
        ))
        providers.append(provider)
        return provider

    monkeypatch.setattr("core.llm.HelloAgentsLLM", make_provider)
    return config, providers, tmp_path


def initialized_pair(app):
    controller = app.state.controller
    sessions = [controller.get_session(controller.create_session(app.state.agent_factory))
                for _ in range(2)]
    agents = [session.ensure_agent() for session in sessions]
    return sessions, agents


def test_default_web_turns_read_and_write_only_their_own_workspace(default_web):
    _, _, root = default_web
    app = create_app(project_root=str(root))
    with TestClient(app):
        _, agents = initialized_pair(app)
        for index, agent in enumerate(agents):
            Path(agent.project_root, "sentinel.txt").write_text(
                f"workspace-{index}", encoding="utf-8")
        for index, agent in enumerate(agents):
            agent.run("Read the sentinel and write output.txt")
            results = [json.loads(m.content) for m in agent.history_manager.get_messages()
                       if m.role == "tool"]
            assert f"workspace-{index}" in results[0]["data"]["content"]
            assert Path(agent.project_root, "output.txt").read_text(encoding="utf-8") == f"agent-{index}"
            assert agent.last_turn_status == "completed"


def test_default_web_owns_permission_and_interaction_bindings(default_web):
    _, _, root = default_web
    app = create_app(project_root=str(root))
    with TestClient(app):
        sessions, agents = initialized_pair(app)
        assert agents[0].tool_registry is not agents[1].tool_registry
        for session, agent in zip(sessions, agents):
            read = agent.tool_registry.get_tool("Read")
            assert read._permission_gate is agent._permission_gate
            assert read._permission_gate._broker._session is session
            assert agent.tool_registry.get_tool("AskUser")._input_func._session is session
        agents[0]._permission_gate._cache["audit-marker"] = "denied"
        assert "audit-marker" not in agents[1]._permission_gate._cache


def test_default_web_supports_two_deferred_discovery_sessions(default_web):
    config, _, root = default_web
    config.tool_deferred_discovery_enabled = True
    app = create_app(project_root=str(root))
    with TestClient(app):
        _, agents = initialized_pair(app)
        assert agents[0]._tool_discovery_state is not agents[1]._tool_discovery_state
        assert agents[0].tool_registry.get_tool("ToolSearch") is not agents[1].tool_registry.get_tool("ToolSearch")


def test_tools_endpoint_projects_live_metadata_without_initializing_sessions(default_web):
    _, providers, root = default_web
    app = create_app(project_root=str(root))
    with TestClient(app) as http:
        http.post("/api/sessions", json={})
        assert http.get("/api/tools").json() == []
        assert providers == []
        _, agents = initialized_pair(app)
        before = [agent.tool_registry.get_tool("Read") for agent in agents]
        names = [item["name"] for item in http.get("/api/tools").json()]
        assert {"Read", "Write", "AskUser"} <= set(names)
        assert names.count("Read") == 1
        assert len(providers) == 2
        assert before == [agent.tool_registry.get_tool("Read") for agent in agents]


def test_closing_one_web_session_preserves_other_sessions_tools(default_web):
    _, _, root = default_web
    app = create_app(project_root=str(root))
    with TestClient(app):
        sessions, agents = initialized_pair(app)
        registry = agents[1].tool_registry
        read = registry.get_tool("Read")
        assert app.state.controller.delete_session(sessions[0].session_id)
        assert registry.get_tool("Read") is read
        assert Path(read._project_root) == Path(agents[1].project_root)


def test_tools_endpoint_keeps_explicit_listing_registry(default_web):
    _, _, root = default_web
    registry = ToolRegistry()

    class CatalogTool(Tool):
        def get_parameters(self):
            return []

        def run(self, parameters):
            raise AssertionError("A metadata request must never run a tool")

    registry.register_tool(CatalogTool(name="CatalogOnly", description="metadata", project_root=root))
    app = create_app(agent_factory=lambda: None, tool_registry=registry, project_root=str(root))
    with TestClient(app) as http:
        assert [item["name"] for item in http.get("/api/tools").json()] == ["CatalogOnly"]


def test_module_entrypoint_uses_independent_session_registries(default_web, monkeypatch):
    import desktop.service.app as service_module

    captured = []
    # Execute the documented module entrypoint without binding a socket.
    monkeypatch.setattr("uvicorn.run", lambda app, **options: captured.append(app))
    runpy.run_path(service_module.__file__, run_name="__main__")
    app, = captured
    with TestClient(app):
        _, agents = initialized_pair(app)
        assert agents[0].tool_registry is not agents[1].tool_registry
