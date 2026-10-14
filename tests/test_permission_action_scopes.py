"""External-path permission prompts must not authorize stronger operations."""
from __future__ import annotations

import json

from tools.builtin.read_file import ReadTool
from tools.builtin.write_file import WriteTool
from tools.permission_gate import PermissionGate


def test_read_approval_does_not_grant_write_or_edit(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    requests = []
    gate = PermissionGate(project_root=project)
    gate._broker = lambda path, tool, action: (
        requests.append((tool, action)) or
        ("granted" if action == "读取" else "denied")
    )

    reader = ReadTool(project_root=project)
    writer = WriteTool(project_root=project)
    reader._permission_gate = gate
    writer._permission_gate = gate

    read_result = json.loads(reader.run({"path": str(outside)}))
    assert read_result["status"] == "success"
    assert gate.check(str(outside), action="读取") == "granted"
    assert gate.check(str(outside), action="写入") == "ask"
    assert gate.check(str(outside), action="编辑") == "ask"

    write_result = json.loads(writer.run({
        "path": "../outside.txt", "content": "MUTATED",
    }))
    assert write_result["status"] == "error"
    assert write_result["error"]["code"] == "ACCESS_DENIED"
    assert outside.read_text() == "original"
    assert requests == [("Read", "读取"), ("Write", "写入")]
    assert gate.check(str(outside), action="编辑") == "denied"
    assert gate.check(str(outside), action="读取") == "granted"


def test_write_and_edit_share_one_mutation_capability(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    gate = PermissionGate(project_root=project)
    gate._broker = lambda *args: "granted"
    assert gate.ask(str(outside), "Write", "写入") == "granted"
    assert gate.check(str(outside), action="编辑") == "granted"
    assert gate.check(str(outside), action="读取") == "ask"
    assert gate.check(str(outside)) == "ask"  # No unscoped cache escalation


def test_subagent_inherits_only_the_approved_operation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    parent = PermissionGate(project_root=project)
    parent._broker = lambda *args: "granted"
    assert parent.ask(str(outside), "Read", "读取") == "granted"

    subagent = parent.subagent_gate()
    assert subagent.check(str(outside), action="读取") == "granted"
    assert subagent.check(str(outside), action="写入") == "denied"
    assert subagent.ask(str(outside), "Edit", "编辑") == "denied"


def test_explicit_legacy_grant_remains_wildcard_but_prompt_is_scoped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    gate = PermissionGate(project_root=project)
    gate.grant(str(outside))
    assert gate.check(str(outside), action="读取") == "granted"
    assert gate.check(str(outside), action="写入") == "granted"
    assert gate.check(str(outside), action="编辑") == "granted"


def test_unknown_operation_cannot_borrow_another_approval(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    gate = PermissionGate(project_root=project)
    gate._broker = lambda *args: "granted"
    assert gate.ask(str(outside), "Read", "读取") == "granted"
    assert gate.check(str(outside), action="execute") == "ask"
    assert gate.check(str(outside), action="write") == "ask"
