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

def test_team_tools_stay_registered_but_are_only_summarized():
    """P2 check: hiding team tools must not make them uncallable."""
    from unittest.mock import MagicMock

    from core.tool_bootstrap import ToolBootstrap, register_team_tools
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    bootstrap = ToolBootstrap(registry=registry, project_root=".")
    for name in ("code_agent", "team_manager", "config", "event_sink", "interactive"):
        bootstrap.provide(name, MagicMock())
    registered = register_team_tools(bootstrap)
    team = [name for name in registry.list_tools() if name.startswith("Team")]
    assert len(team) >= 14
    assert len(registered) == len(team) + 1  # + SendMessage

    notes = ContextBuilder(tool_registry=registry, project_root=".")._tool_usage_notes()
    assert "Also available:" in notes
    for name in team:
        assert name in notes, "team tool must stay discoverable"
        assert f"{name}:" not in notes, "team notes must not be expanded"

    expanded = sum(
        len(getattr(registry.get_tool(name), "usage_notes", "") or "")
        for name in registry.list_tools()
    )
    assert len(notes) < expanded
    # The whole deferred footer stays small relative to expanded team notes.
    team_notes = sum(
        len(getattr(registry.get_tool(name), "usage_notes", "") or "") for name in team
    )
    assert team_notes > 500
    assert len(notes) < expanded - team_notes + 400

def test_discovery_visibility_derives_from_the_manifest():
    """One source of truth: schemas hidden by discovery == deferred tools."""
    from core.runtime.tool_catalog import DEFAULT_ALWAYS_VISIBLE
    from tools.tool_specs import CORE_TOOLS

    assert DEFAULT_ALWAYS_VISIBLE == CORE_TOOLS | {"ToolSearch"}
    for deferred in ("MultiEdit", "LS", "Workflow", "TeamFanout", "TeamCollect"):
        assert deferred not in DEFAULT_ALWAYS_VISIBLE
    for core in ("Read", "Bash", "WebSearch", "WebFetch", "Goal", "Memory"):
        assert core in DEFAULT_ALWAYS_VISIBLE
