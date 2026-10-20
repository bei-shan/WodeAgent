"""Dynamic Skill actions must agree with the published model schema."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

from tools.builtin.skill import SkillTool
from tools.registry import ToolRegistry


def test_skill_list_without_name_via_registry(tmp_path):
    loader = Mock()
    loader.list_skills.return_value = [
        SimpleNamespace(name="review", description="Check code")
    ]
    tool = SkillTool(project_root=tmp_path, skill_loader=loader)
    registry = ToolRegistry()
    registry.register_tool(tool)
    out = json.loads(registry.execute_tool("Skill", {"action": "list"}))
    assert out["status"] == "success", out
    assert out["data"]["skills"][0]["name"] == "review"
    schema = next(
        x for x in registry.get_openai_tools()
        if x["function"]["name"] == "Skill"
    )
    assert "name" not in schema["function"]["parameters"].get("required", [])


def test_skill_invalid_inputs_are_tool_errors_not_uncaught_exceptions(tmp_path):
    loader = Mock()
    tool = SkillTool(project_root=tmp_path, skill_loader=loader)
    for payload in [
        {"action": 23},
        {"action": "unknown"},
        {"action": "load", "name": "review", "args": ["bad"]},
        {"action": "create", "name": "new", "description": [], "content": "text"},
        {"action": "create", "name": "new", "description": "okay", "content": {}},
        {"action": "load", "name": "review", "args": "A" * 5000},
    ]:
        out = json.loads(tool.run(payload))
        assert out["status"] == "error", out
        assert out["error"]["code"] == "INVALID_PARAM"
    loader.create_skill.assert_not_called()


def test_skill_load_rejects_oversized_instruction_file(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    skill_file = root / "SKILL.md"
    skill_file.write_text(
        "---\nname: review\ndescription: Test\n---\n"
        + "Z" * 300000
    )
    loader = Mock()
    loader.get_skill.return_value = SimpleNamespace(
        name="review", path=str(skill_file), base_dir=str(root)
    )
    loader._safe_skill_path.return_value = True
    tool = SkillTool(project_root=tmp_path, skill_loader=loader)
    out = json.loads(tool.run({"name": "review"}))
    assert out["status"] == "error"
    assert out["error"]["code"] == "INVALID_PARAM"
    assert "256 KiB" in out["error"]["message"]
