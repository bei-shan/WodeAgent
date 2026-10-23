"""Memory tool: gating, CRUD through the tool protocol, and agent-side hiding."""
from __future__ import annotations

import json
import logging

import pytest

from agents.codeAgent import CodeAgent
from core.memory.settings import MemorySettings, save_settings
from tools.builtin.memory import MemoryTool


def _tool(tmp_path) -> MemoryTool:
    return MemoryTool(project_root=str(tmp_path))


def _enable(tmp_path) -> None:
    save_settings(tmp_path, MemorySettings(enabled=True, provider="sqlite", limit=8))


def test_disabled_memory_reports_how_to_enable(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))
    payload = json.loads(_tool(tmp_path).run({"action": "search", "query": "x"}))
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "EXECUTION_ERROR"
    assert "记忆" in payload["error"]["message"]


def test_save_search_list_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))
    _enable(tmp_path)
    tool = _tool(tmp_path)

    saved = json.loads(tool.run({"action": "save", "content": "Use pnpm for this repo"}))
    assert saved["status"] == "success"
    record_id = saved["data"]["record"]["id"]

    found = json.loads(tool.run({"action": "search", "query": "pnpm"}))
    assert [item["id"] for item in found["data"]["records"]] == [record_id]

    listed = json.loads(tool.run({"action": "list"}))
    assert len(listed["data"]["records"]) == 1
    assert "Use pnpm for this repo" in listed["text"]

    deleted = json.loads(tool.run({"action": "delete", "record_id": record_id}))
    assert deleted["data"]["deleted"] is True
    assert json.loads(tool.run({"action": "delete", "record_id": record_id}))["data"]["deleted"] is False


def test_tool_rejects_bad_input(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))
    _enable(tmp_path)
    tool = _tool(tmp_path)

    assert json.loads(tool.run({"action": "explode"}))["error"]["code"] == "INVALID_PARAM"
    assert json.loads(tool.run({"action": "save", "content": ""}))["error"]["code"] == "INVALID_PARAM"


class _Registry:
    def __init__(self):
        self.removed: list[str] = []

    def unregister(self, name, *, owner="legacy"):
        self.removed.append(name)


class _FakeAgent:
    def __init__(self, project_root):
        self.project_root = str(project_root)
        self.tool_registry = _Registry()
        self.logger = logging.getLogger("memory-gate-test")


def test_agent_hides_memory_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))
    fake = _FakeAgent(tmp_path)
    CodeAgent._disable_optional_tools(fake, ["Memory"])
    assert fake.tool_registry.removed == ["Memory"]


def test_agent_keeps_memory_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODEAGENT_DATA_DIR", str(tmp_path / "data"))
    _enable(tmp_path)
    fake = _FakeAgent(tmp_path)
    CodeAgent._disable_optional_tools(fake, ["Memory"])
    assert fake.tool_registry.removed == []


def test_unknown_tools_are_untouched(tmp_path, monkeypatch):
    fake = _FakeAgent(tmp_path)
    CodeAgent._disable_optional_tools(fake, ["Bash", "Read"])
    assert fake.tool_registry.removed == []
