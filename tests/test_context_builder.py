"""ContextBuilder tests."""

import time
import unittest
from types import SimpleNamespace

from core.context_engine.context_builder import ContextBuilder
from tests.utils.test_helpers import create_temp_project


class DummyToolRegistry:
    def get_all_tools(self):
        return []


class TestContextBuilder(unittest.TestCase):
    def _make_project(self, structure):
        return create_temp_project(structure)

    def test_plan_mode_guidance_marks_the_tool_as_inactive(self):
        """Plan mode is entered by calling EnterPlanMode, so a session that has
        not called it must not be told it is already read-only."""
        structure = {
            "prompts/agents_prompts/L1_system_prompt.md": "L1 {tools}",
        }
        with self._make_project(structure) as project:
            registry = DummyToolRegistry()
            registry.get_all_tools = lambda: [
                SimpleNamespace(name="EnterPlanMode", usage_notes="")
            ]
            builder = ContextBuilder(
                tool_registry=registry, project_root=str(project.root), trusted_prompt_root=str(project.root)
            )
            text = builder._build_l1()
            self.assertIn("# Plan Mode", text)
            block = text[text.index("# Plan Mode"):]
            self.assertIn("not active", block.lower())

    def test_build_messages_with_history(self):
        structure = {
            "prompts/agents_prompts/L1_system_prompt.md": "L1 {tools}",
            "prompts/tools_prompts/ls_prompt.py": "ls_prompt = 'LS tool'",
            "CODE_LAW.md": "Rule A",
        }
        with self._make_project(structure) as project:
            builder = ContextBuilder(tool_registry=DummyToolRegistry(), project_root=str(project.root), trusted_prompt_root=str(project.root))
            history = [{"role": "user", "content": "hi"}]
            messages = builder.build_messages(history)
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[1]["role"], "system")
            self.assertEqual(messages[2]["role"], "user")  # CODE_LAW is data
            self.assertEqual(messages[3], history[0])

    def test_system_prompt_override(self):
        """Operator override is system text; tool notes remain low trust."""
        structure = {}
        with self._make_project(structure) as project:
            from tools.builtin.list_files import ListFilesTool
            registry = DummyToolRegistry()
            registry.get_all_tools = lambda: [ListFilesTool(project_root=str(project.root), trusted_prompt_root=str(project.root))]
            builder = ContextBuilder(
                tool_registry=registry,
                project_root=str(project.root), trusted_prompt_root=str(project.root),
                system_prompt_override="OVERRIDE {tools}",
            )
            messages = builder.get_system_messages()
            self.assertIn("OVERRIDE", messages[0]["content"])
            self.assertNotIn("LS:", "\n".join(m["content"] for m in messages))
            refs = builder.get_reference_messages()
            self.assertTrue(any(m["role"] == "user" and "LS:" in m["content"] for m in refs))

    def test_code_law_lowercase_name(self):
        structure = {
            "prompts/agents_prompts/L1_system_prompt.md": "L1",
            "code_law.md": "lowercase rule",
        }
        with self._make_project(structure) as project:
            builder = ContextBuilder(tool_registry=DummyToolRegistry(), project_root=str(project.root), trusted_prompt_root=str(project.root))
            messages = builder.build_messages([])
            refs = [m for m in messages if "lowercase rule" in m["content"]]
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["role"], "user")

    def test_code_law_cache_refresh(self):
        """CODE_LAW uses mtime cache — changes detected on file modification."""
        structure = {
            "prompts/agents_prompts/L1_system_prompt.md": "L1",
            "CODE_LAW.md": "Rule A",
        }
        with self._make_project(structure) as project:
            builder = ContextBuilder(tool_registry=DummyToolRegistry(), project_root=str(project.root), trusted_prompt_root=str(project.root))
            messages1 = builder.build_messages([])
            self.assertTrue(any("Rule A" in m["content"] for m in messages1))
            time.sleep(0.01)
            project.path("CODE_LAW.md").write_text("Rule B", encoding="utf-8")
            # Late Binding: mtime changed → auto-detected, no manual invalidation needed
            messages2 = builder.build_messages([])
            self.assertTrue(any("Rule B" in m["content"] for m in messages2))

    def test_tool_prompts_from_usage_notes(self):
        """Tool prompts come from Tool.usage_notes, not from prompt files."""
        structure = {
            "prompts/agents_prompts/L1_system_prompt.md": "L1 {tools}",
        }
        with self._make_project(structure) as project:
            from tools.builtin.read_file import ReadTool
            from tools.builtin.bash import BashTool
            registry = DummyToolRegistry()
            registry.get_all_tools = lambda: [
                ReadTool(project_root=str(project.root), trusted_prompt_root=str(project.root)),
                BashTool(project_root=str(project.root), trusted_prompt_root=str(project.root)),
            ]
            builder = ContextBuilder(tool_registry=registry, project_root=str(project.root), trusted_prompt_root=str(project.root))
            system = builder.get_system_messages()
            system_text = "\n".join(m["content"] for m in system)
            self.assertNotIn("Read:", system_text)
            self.assertNotIn("Bash:", system_text)
            refs = builder.get_reference_messages()
            reference_text = "\n".join(m["content"] for m in refs)
            self.assertIn("Read:", reference_text)
            self.assertIn("Bash:", reference_text)
            self.assertTrue(all(m["role"] == "user" for m in refs))

    def test_missing_tool_prompts_dir(self):
        structure = {
            "prompts/agents_prompts/L1_system_prompt.md": "L1 {tools}",
        }
        with self._make_project(structure) as project:
            builder = ContextBuilder(tool_registry=DummyToolRegistry(), project_root=str(project.root), trusted_prompt_root=str(project.root))
            messages = builder.get_system_messages()
            self.assertTrue(any(m["role"] == "system" for m in messages))
            self.assertNotIn("Available Tools", messages[0]["content"])

    def test_mcp_tools_prompt_injection(self):
        """MCP tool descriptions stay as lower-priority reference data."""
        structure = {
            "prompts/agents_prompts/L1_system_prompt.md": "L1",
        }
        with self._make_project(structure) as project:
            builder = ContextBuilder(tool_registry=DummyToolRegistry(), project_root=str(project.root), trusted_prompt_root=str(project.root))
            builder.set_mcp_tools_prompt("MCP tool list")
            messages = builder.build_messages([])
            mcp_msgs = [m for m in messages if "MCP tool list" in m.get("content", "")]
            self.assertEqual(len(mcp_msgs), 1)
            self.assertEqual(mcp_msgs[0]["role"], "user")

    def test_skills_prompt_injection(self):
        structure = {
            "prompts/agents_prompts/L1_system_prompt.md": "L1 {tools}",
            "prompts/tools_prompts/skill_prompt.py": "skill_prompt = 'Skills: {{available_skills}}'",
        }
        with self._make_project(structure) as project:
            builder = ContextBuilder(tool_registry=DummyToolRegistry(), project_root=str(project.root), trusted_prompt_root=str(project.root))
            builder.set_skills_prompt("SkillA, SkillB")
            messages = builder.build_messages([])
            self.assertTrue(any(m["role"] == "user" and "SkillA, SkillB" in m["content"] for m in messages))

    def test_cache_invalidated_on_set_skills_prompt(self):
        structure = {
            "prompts/agents_prompts/L1_system_prompt.md": "L1 {tools}",
            "prompts/tools_prompts/skill_prompt.py": "skill_prompt = 'Skills: {{available_skills}}'",
        }
        with self._make_project(structure) as project:
            builder = ContextBuilder(tool_registry=DummyToolRegistry(), project_root=str(project.root), trusted_prompt_root=str(project.root))
            messages1 = builder.build_messages([])
            self.assertFalse(any("SkillA" in m["content"] for m in messages1))
            builder.set_skills_prompt("SkillA")
            messages2 = builder.build_messages([])
            self.assertTrue(any("SkillA" in m["content"] for m in messages2))


if __name__ == "__main__":
    unittest.main()
