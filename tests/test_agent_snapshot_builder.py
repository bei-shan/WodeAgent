"""Snapshot assembly has one canonical v2 schema across both save paths."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from agents.codeAgent import CodeAgent
from core.runtime.session_snapshot import SessionSnapshotBuilder


def test_snapshot_builder_projects_tree_fields_and_team_state():
    history = SimpleNamespace(
        serialize_messages=lambda: [{"role": "user", "content": "hello"}],
        serialize_entries=lambda: [{"id": "m1", "role": "user", "content": "hello"}],
        get_cursor_id=lambda: "m1",
        _labels={"checkpoint": "m1"},
        get_current_model=lambda: {"modelId": "test"},
        get_thinking_level=lambda: "off",
    )
    host = SimpleNamespace(
        project_root="/tmp/agent-test",
        history_manager=history,
        context_builder=SimpleNamespace(_load_code_law=lambda: ""),
        tool_registry=SimpleNamespace(export_read_cache=lambda: {}),
        team_manager=SimpleNamespace(export_state=lambda: {"work_items": {"alpha": {"queued": 1}}}),
        team_store_dir=".teams",
        task_store_dir=".tasks",
        _active_worktree=None,
        _skills_prompt="",
        _mcp_tools_prompt="",
        _get_system_messages_for_run=lambda: [{"role": "system", "content": "test"}],
        _get_openai_tools_for_current_mode=lambda: [],
    )
    snapshot = SessionSnapshotBuilder(host).capture()
    assert snapshot["schema_version"] == 2
    assert snapshot["cursor_id"] == "m1"
    assert snapshot["history_entries"][0]["id"] == "m1"
    assert snapshot["parallel_work_index"]["alpha"]["queued"] == 1
    assert snapshot["teams_snapshot"]["work_items"]["alpha"]["queued"] == 1


def test_legacy_save_delegates_to_same_snapshot_builder(monkeypatch, tmp_path):
    result = []
    monkeypatch.setattr(
        "agents.codeAgent.save_session_snapshot",
        lambda path, snapshot: result.append((path, snapshot)),
    )
    agent = CodeAgent.__new__(CodeAgent)
    agent._build_snapshot = lambda: {"schema_version": 2, "cursor_id": "m1"}
    path = str(tmp_path / "session.json")
    agent.save_session(path)
    assert result == [(path, {"schema_version": 2, "cursor_id": "m1"})]


def test_snapshot_builder_has_no_codeagent_or_web_dependency():
    source = (Path(__file__).resolve().parents[1] /
              "core/runtime/session_snapshot.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("agents", "desktop", "tui", "fastapi"))
