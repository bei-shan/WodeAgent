"""Tests for ToolBootstrap auto-discovery and DI container."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.base import Tool, ToolParameter, ToolStatus, ErrorCode
from tools.registry import ToolRegistry
from core.tool_bootstrap import ToolBootstrap, register_team_tools


# ---------------------------------------------------------------------------
# Minimal test tool
# ---------------------------------------------------------------------------

class _SimpleTool(Tool):
    """A tool that only needs project_root."""
    def __init__(self, name="Simple", project_root=None, working_dir=None):
        super().__init__(name=name, description="Simple test tool",
                         project_root=project_root, working_dir=working_dir)

    def get_parameters(self):
        return []

    def run(self, parameters):
        return self.create_success_response({"ok": True}, "Done", {"time_ms": 1},
                                            {"cwd": ".", "params_input": parameters})


class _ToolWithDeps(Tool):
    """A tool that needs code_agent and skill_loader."""
    def __init__(self, name="WithDeps", project_root=None, working_dir=None,
                 code_agent=None, skill_loader=None, optional_extra=None):
        super().__init__(name=name, description="Tool with deps",
                         project_root=project_root, working_dir=working_dir)
        self._code_agent = code_agent
        self._skill_loader = skill_loader
        self._optional_extra = optional_extra

    def get_parameters(self):
        return []

    def run(self, parameters):
        if not self._code_agent or not self._skill_loader:
            return self.create_error_response(ErrorCode.INTERNAL_ERROR,
                                              "Missing deps", {"time_ms": 1},
                                              {"cwd": ".", "params_input": parameters})
        return self.create_success_response({"ok": True}, "Done", {"time_ms": 1},
                                            {"cwd": ".", "params_input": parameters})


# ---------------------------------------------------------------------------
# DI container tests
# ---------------------------------------------------------------------------

def test_provide_injects_dependency():
    bootstrap = ToolBootstrap(registry=MagicMock(), project_root="/tmp/test")
    bootstrap.provide("code_agent", "fake-agent")
    assert bootstrap._providers["code_agent"] == "fake-agent"


def test_instantiate_simple_tool():
    """Tools with only project_root can be instantiated."""
    bootstrap = ToolBootstrap(registry=MagicMock(), project_root="/tmp/test")
    instance = bootstrap._instantiate(_SimpleTool)
    assert instance.name == "Simple"
    assert str(instance._project_root) == str(Path("/tmp/test").resolve())


def test_instantiate_injects_known_deps():
    """Known dependencies are injected by name."""
    bootstrap = ToolBootstrap(registry=MagicMock(), project_root="/tmp/test")
    bootstrap.provide("code_agent", "fake-agent")
    bootstrap.provide("skill_loader", "fake-loader")

    instance = bootstrap._instantiate(_ToolWithDeps)
    assert instance._code_agent == "fake-agent"
    assert instance._skill_loader == "fake-loader"
    assert instance._optional_extra is None  # not provided → default


def test_instantiate_missing_optional_dep_is_none():
    """Unprovided optional deps get None (their default)."""
    bootstrap = ToolBootstrap(registry=MagicMock(), project_root="/tmp/test")
    # Don't provide code_agent or skill_loader
    instance = bootstrap._instantiate(_ToolWithDeps)
    assert instance._code_agent is None
    assert instance._skill_loader is None


def test_project_root_always_injected():
    """project_root is always injected even without explicit provide()."""
    bootstrap = ToolBootstrap(registry=MagicMock(), project_root="/custom/root")
    instance = bootstrap._instantiate(_SimpleTool)
    assert str(instance._project_root) == str(Path("/custom/root").resolve())


def test_working_dir_defaults_to_project_root():
    """working_dir defaults to project_root when not explicitly provided."""
    bootstrap = ToolBootstrap(registry=MagicMock(), project_root="/tmp/test")
    instance = bootstrap._instantiate(_SimpleTool)
    assert str(instance._working_dir) == str(Path("/tmp/test").resolve())


# ---------------------------------------------------------------------------
# discover_and_register tests
# ---------------------------------------------------------------------------

def test_discover_registers_all_builtin_tools():
    """Scanning tools/builtin/ registers all non-team tools."""
    registry = ToolRegistry()
    bootstrap = ToolBootstrap(registry=registry, project_root="/tmp/test")
    bootstrap.provide("code_agent", "fake-agent")
    bootstrap.provide("main_llm", MagicMock())
    bootstrap.provide("tool_registry", registry)
    bootstrap.provide("team_manager", None)
    bootstrap.provide("background_runner", MagicMock())  # TaskOutputTool requires this
    bootstrap.provide("skill_loader", MagicMock())
    bootstrap.provide("interactive", True)
    bootstrap.provide("worktree_manager", MagicMock())

    registered = bootstrap.discover_and_register()

    # All 17 non-team tools should be registered (SwitchModel removed — user-driven /model command only)
    assert len(registered) >= 17
    tool_names = set(registry.list_tools())
    assert "Read" in tool_names
    assert "Write" in tool_names
    assert "Edit" in tool_names
    assert "MultiEdit" in tool_names
    assert "LS" in tool_names
    assert "Glob" in tool_names
    assert "Grep" in tool_names
    assert "Bash" in tool_names
    assert "TodoWrite" in tool_names
    assert "Skill" in tool_names
    assert "AskUser" in tool_names
    assert "Task" in tool_names
    assert "TaskOutput" in tool_names
    assert "EnterPlanMode" in tool_names
    assert "ExitPlanMode" in tool_names
    assert "EnterWorktree" in tool_names
    assert "ExitWorktree" in tool_names
    assert "SwitchModel" not in tool_names  # removed: model switching is user-driven via /model


def test_discover_skips_team_tools():
    """Team tools are NOT registered by discover_and_register()."""
    registry = ToolRegistry()
    bootstrap = ToolBootstrap(registry=registry, project_root="/tmp/test")

    bootstrap.discover_and_register()
    tool_names = set(registry.list_tools())

    # Team tools should be absent
    assert "TeamCreate" not in tool_names
    assert "SendMessage" not in tool_names
    assert "TeamStatus" not in tool_names


def test_register_team_tools_registers_all_15():
    """register_team_tools() registers all 15 team tools."""
    registry = ToolRegistry()
    bootstrap = ToolBootstrap(registry=registry, project_root="/tmp/test")
    bootstrap.provide("team_manager", MagicMock())

    registered = register_team_tools(bootstrap)

    assert len(registered) == 15
    tool_names = set(registry.list_tools())
    assert "TeamCreate" in tool_names
    assert "SendMessage" in tool_names
    assert "TeamStatus" in tool_names
    assert "TeamDelete" in tool_names
    assert "TeamCleanup" in tool_names
    assert "TeamApprovals" in tool_names
    assert "TeamApprovePlan" in tool_names
    assert "TeamFanout" in tool_names
    assert "TeamCollect" in tool_names
    assert "TeamTaskCreate" in tool_names
    assert "TeamTaskGet" in tool_names
    assert "TeamTaskUpdate" in tool_names
    assert "TeamTaskList" in tool_names
    assert "TeamList" in tool_names
    assert "TeamRetry" in tool_names


def test_register_team_tools_without_team_manager_skips():
    """Without team_manager provider, team tools are skipped gracefully."""
    registry = ToolRegistry()
    bootstrap = ToolBootstrap(registry=registry, project_root="/tmp/test")
    # Don't provide team_manager

    registered = register_team_tools(bootstrap)
    # May register 0 or log warnings, but shouldn't crash
    assert isinstance(registered, list)


def test_discover_then_team_tools_no_duplicates():
    """Running discover_and_register then register_team_tools produces no duplicates."""
    registry = ToolRegistry()
    bootstrap = ToolBootstrap(registry=registry, project_root="/tmp/test")
    bootstrap.provide("code_agent", "fake-agent")
    bootstrap.provide("main_llm", MagicMock())
    bootstrap.provide("tool_registry", registry)
    bootstrap.provide("team_manager", MagicMock())
    bootstrap.provide("background_runner", MagicMock())
    bootstrap.provide("skill_loader", MagicMock())
    bootstrap.provide("interactive", True)
    bootstrap.provide("worktree_manager", MagicMock())

    non_team = bootstrap.discover_and_register()
    team = register_team_tools(bootstrap)

    all_tools = set(registry.list_tools())
    # No overlap between non-team and team
    assert len(set(non_team) & set(team)) == 0
    # Total should be 17 non-team + 15 team = 32 (SwitchModel removed)
    assert len(all_tools) == 32


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_discover_handles_broken_module_gracefully():
    """A broken module doesn't crash the whole discovery."""
    registry = ToolRegistry()
    bootstrap = ToolBootstrap(registry=registry, project_root="/tmp/test")

    # Should not raise — just log a warning
    registered = bootstrap.discover_and_register()
    assert isinstance(registered, list)


def test_provide_overwrites_previous():
    """Calling provide() twice overwrites the previous value."""
    bootstrap = ToolBootstrap(registry=MagicMock(), project_root="/tmp/test")
    bootstrap.provide("code_agent", "first")
    bootstrap.provide("code_agent", "second")
    assert bootstrap._providers["code_agent"] == "second"


# ---------------------------------------------------------------------------
# Real tool registration integration
# ---------------------------------------------------------------------------

def test_real_tool_registration_via_bootstrap():
    """All 33 real tools can be instantiated and registered via ToolBootstrap."""
    registry = ToolRegistry()
    bootstrap = ToolBootstrap(registry=registry, project_root="/tmp/test")
    bootstrap.provide("code_agent", MagicMock())
    bootstrap.provide("main_llm", MagicMock())
    bootstrap.provide("tool_registry", registry)
    bootstrap.provide("team_manager", MagicMock())
    bootstrap.provide("background_runner", MagicMock())
    bootstrap.provide("skill_loader", MagicMock())
    bootstrap.provide("interactive", True)
    bootstrap.provide("worktree_manager", MagicMock())

    non_team = bootstrap.discover_and_register()
    team = register_team_tools(bootstrap)

    assert len(non_team) == 17  # SwitchModel removed
    assert len(team) == 15

    # All tools are executable (run returns valid JSON)
    for tool_name in registry.list_tools():
        result = registry.execute_tool(tool_name, "{}")
        assert result is not None
        import json
        try:
            parsed = json.loads(result)
            assert "status" in parsed
        except json.JSONDecodeError:
            pytest.fail(f"Tool {tool_name} returned invalid JSON: {result[:200]}")
