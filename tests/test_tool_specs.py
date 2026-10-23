"""Tool manifest: metadata table, prompt exposure and the inventory API."""
from __future__ import annotations

from fastapi.testclient import TestClient

from core.context_engine.context_builder import ContextBuilder
from desktop.service.app import create_app
from tests.test_web_service import FakeAgent
from tools.tool_specs import SPECS, ToolSpec, describe_tool, spec_for


class _Tool:
    def __init__(self, name, notes="", description="d"):
        self.name = name
        self.usage_notes = notes
        self.description = description


class _Registry:
    def __init__(self, tools):
        self._tools = tools

    def get_all_tools(self):
        return self._tools


def test_manifest_covers_every_registered_tool_name():
    expected = {
        "Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep", "LS",
        "Task", "TaskOutput", "SendMessage", "Workflow", "BrowserUse",
        "WebFetch", "WebSearch", "AskUser", "Skill", "TodoWrite", "Goal",
        "Memory", "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    }
    assert expected <= set(SPECS)


def test_spec_defaults_and_overrides():
    assert spec_for("Read").read_only is True
    assert spec_for("Bash").approval == "ask"
    assert spec_for("LS").exposure == "deferred"
    assert spec_for("NotARealTool").kind == "control"

    class Custom:
        name = "Custom"
        description = "custom tool"
        spec = ToolSpec(kind="read", read_only=True, group="x")

    entry = describe_tool(Custom())
    assert entry["kind"] == "read" and entry["read_only"] is True
    assert entry["group"] == "x"
    assert entry["name"] == "Custom"


def test_deferred_tools_are_summarized_not_expanded():
    registry = _Registry([
        _Tool("Read", notes="READ NOTES"),
        _Tool("LS", notes="LS NOTES SHOULD NOT APPEAR"),
        _Tool("Workflow", notes="WORKFLOW NOTES SHOULD NOT APPEAR"),
    ])
    builder = ContextBuilder(tool_registry=registry, project_root=".")
    notes = builder._tool_usage_notes()
    assert "READ NOTES" in notes
    assert "LS NOTES" not in notes
    assert "Workflow" in notes
    assert "Also available:" in notes


def test_tools_api_returns_groups(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    with TestClient(create_app(agent_factory=FakeAgent, project_root=str(tmp_path))) as client:
        payload = client.get("/api/tools").json()
        names = {entry["name"] for entry in payload["tools"]}
        assert {"Read", "Bash", "WebSearch", "Goal"} <= names
        assert "WebSearch" in payload["core"]
        assert payload["groups"]["files"]
        read = next(entry for entry in payload["tools"] if entry["name"] == "Read")
        assert read["kind"] == "read" and read["read_only"] is True
