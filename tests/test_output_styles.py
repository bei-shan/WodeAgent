"""Tests for Output Styles feature."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.output_styles import (
    OutputStyleManager,
    OutputStyleDefinition,
    DEFAULT_STYLE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(styles_dir_name: str = "output_styles") -> str:
    """Create a temporary project root with the given custom-styles directory."""
    tmp = tempfile.mkdtemp(prefix="output_styles_test_")
    (Path(tmp) / "prompts" / "output_styles").mkdir(parents=True)
    (Path(tmp) / styles_dir_name).mkdir(parents=True, exist_ok=True)
    return tmp


def _write_builtin_style(project_root: str, name: str, content: str) -> None:
    path = Path(project_root) / "prompts" / "output_styles" / f"{name}.md"
    path.write_text(content, encoding="utf-8")


def _write_custom_style(project_root: str, name: str, content: str) -> None:
    path = Path(project_root) / "output_styles" / f"{name}.md"
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Real project styles (load from the actual prompts/output_styles/ dir)
# ---------------------------------------------------------------------------

@pytest.fixture
def real_project_root():
    """Return the actual project root so we can test real style files."""
    return Path(__file__).resolve().parent.parent


class TestRealBuiltinStyles:
    """Tests using the actual built-in style files."""

    def test_default_style_has_empty_prompt(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        assert mgr.get_current_prompt() == ""

    def test_explanatory_style_has_prompt(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        mgr.set_current("explanatory")
        prompt = mgr.get_current_prompt()
        assert len(prompt) > 50
        assert "Output Style: explanatory" in prompt
        assert "Insight" in prompt

    def test_learning_style_has_prompt(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        mgr.set_current("learning")
        prompt = mgr.get_current_prompt()
        assert len(prompt) > 100
        assert "Output Style: learning" in prompt

    def test_learning_includes_insights(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        mgr.set_current("learning")
        prompt = mgr.get_current_prompt()
        assert "★ Insight" in prompt or "Insight" in prompt

    def test_learning_includes_learn_by_doing(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        mgr.set_current("learning")
        prompt = mgr.get_current_prompt()
        assert "Learn by Doing" in prompt
        assert "TODO(human)" in prompt

    def test_three_builtin_styles_exist(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        all_styles = mgr.list_all()
        assert "default" in all_styles
        assert "explanatory" in all_styles
        assert "learning" in all_styles


# ---------------------------------------------------------------------------
# Manager API
# ---------------------------------------------------------------------------

class TestManagerAPI:
    """Tests for OutputStyleManager public API."""

    def test_resolve_name_case_insensitive(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        assert mgr.resolve_name("Explanatory") == "explanatory"
        assert mgr.resolve_name("LEARNING") == "learning"
        assert mgr.resolve_name("Default") == "default"

    def test_resolve_name_invalid_returns_none(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        assert mgr.resolve_name("nonexistent") is None
        assert mgr.resolve_name("") is None

    def test_set_current_valid(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        assert mgr.set_current("explanatory") is True
        assert mgr.get_current() == "explanatory"

    def test_set_current_invalid(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        assert mgr.set_current("nonexistent") is False
        assert mgr.get_current() == "default"  # unchanged

    def test_list_all_returns_dict(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        result = mgr.list_all()
        assert isinstance(result, dict)
        assert all(isinstance(v, str) for v in result.values())

    def test_get_style_returns_definition(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        style = mgr.get_style("explanatory")
        assert style is not None
        assert style.name == "explanatory"
        assert style.source == "builtin"
        assert style.keep_coding_instructions is True

    def test_get_style_nonexistent_returns_none(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        assert mgr.get_style("nonexistent") is None


# ---------------------------------------------------------------------------
# Custom styles
# ---------------------------------------------------------------------------

class TestCustomStyles:
    """Tests for project-level custom styles."""

    def test_custom_style_override_builtin(self):
        root = _make_project()
        # Write a built-in explanatory and a project-level override.
        _write_builtin_style(root, "explanatory",
            "---\nname: explanatory\ndescription: builtin\n---\nbuiltin prompt")
        _write_custom_style(root, "explanatory",
            "---\nname: explanatory\ndescription: custom override\n---\ncustom prompt")

        mgr = OutputStyleManager(project_root=root)
        mgr.set_current("explanatory")
        prompt = mgr.get_current_prompt()
        assert "custom prompt" in prompt
        assert "builtin prompt" not in prompt

    def test_custom_style_from_md(self):
        root = _make_project()
        _write_custom_style(root, "concise",
            "---\nname: concise\ndescription: very concise\nkeep_coding_instructions: false\n---\nBe brief.")

        mgr = OutputStyleManager(project_root=root)
        style = mgr.get_style("concise")
        assert style is not None
        assert style.name == "concise"
        assert style.description == "very concise"
        assert style.prompt == "Be brief."
        assert style.source == "project"
        assert style.keep_coding_instructions is False

    def test_custom_style_missing_frontmatter(self):
        root = _make_project()
        _write_custom_style(root, "plain", "Just some prompt text.")

        mgr = OutputStyleManager(project_root=root)
        style = mgr.get_style("plain")
        assert style is not None
        assert style.name == "plain"
        assert style.prompt == "Just some prompt text."
        assert style.source == "project"
        # keep_coding_instructions defaults to True when no frontmatter
        assert style.keep_coding_instructions is True

    def test_custom_style_keeps_default_when_no_prompt(self):
        root = _make_project()
        mgr = OutputStyleManager(project_root=root)
        assert mgr.get_current() == "default"
        assert mgr.get_current_prompt() == ""


# ---------------------------------------------------------------------------
# Env var
# ---------------------------------------------------------------------------

class TestEnvVar:
    """Tests for AGENT_OUTPUT_STYLE env var."""

    def test_env_var_sets_initial_style(self, real_project_root):
        mgr = OutputStyleManager(
            project_root=str(real_project_root),
            env_style="explanatory",
        )
        assert mgr.get_current() == "explanatory"

    def test_env_var_invalid_falls_back_to_default(self, real_project_root):
        mgr = OutputStyleManager(
            project_root=str(real_project_root),
            env_style="nonexistent",
        )
        assert mgr.get_current() == "default"

    def test_env_var_empty_string_defaults(self, real_project_root):
        mgr = OutputStyleManager(
            project_root=str(real_project_root),
            env_style="",
        )
        assert mgr.get_current() == "default"

    def test_env_var_none_defaults(self, real_project_root):
        mgr = OutputStyleManager(
            project_root=str(real_project_root),
            env_style=None,
        )
        assert mgr.get_current() == "default"


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------

class TestReload:
    """Tests for the reload() method."""

    def test_reload_discovers_new_styles(self):
        root = _make_project()
        mgr = OutputStyleManager(project_root=root)
        assert mgr.get_style("newstyle") is None

        _write_custom_style(root, "newstyle",
            "---\nname: newstyle\ndescription: newly added\n---\nnew prompt")

        mgr.reload()
        style = mgr.get_style("newstyle")
        assert style is not None
        assert style.name == "newstyle"
        assert style.prompt == "new prompt"


# ---------------------------------------------------------------------------
# ContextBuilder integration
# ---------------------------------------------------------------------------

class TestContextBuilderIntegration:
    """Tests that the {output_style} placeholder is correctly replaced."""

    def test_context_builder_injects_style_prompt(self, real_project_root):
        from core.context_engine.context_builder import ContextBuilder
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        builder = ContextBuilder(
            tool_registry=registry,
            project_root=str(real_project_root),
        )

        # Simulate what CodeAgent does.
        mgr = OutputStyleManager(
            project_root=str(real_project_root),
            env_style="explanatory",
        )
        builder.set_output_style_prompt(mgr.get_current_prompt())

        # Load system messages — the style prompt should be inside L1.
        msgs = builder._get_system_messages()
        l1_content = msgs[0]["content"] if msgs else ""

        assert "Output Style: explanatory" in l1_content
        assert "Insight" in l1_content

    def test_context_builder_skips_default_style(self, real_project_root):
        from core.context_engine.context_builder import ContextBuilder
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        builder = ContextBuilder(
            tool_registry=registry,
            project_root=str(real_project_root),
        )

        mgr = OutputStyleManager(project_root=str(real_project_root))
        assert mgr.get_current() == "default"
        builder.set_output_style_prompt(mgr.get_current_prompt())

        msgs = builder._get_system_messages()
        l1_content = msgs[0]["content"] if msgs else ""

        # Default style — should NOT have Output Style marker.
        assert "Output Style:" not in l1_content

    def test_set_output_style_prompt_invalidates_cache(self, real_project_root):
        from core.context_engine.context_builder import ContextBuilder
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        builder = ContextBuilder(
            tool_registry=registry,
            project_root=str(real_project_root),
        )

        mgr = OutputStyleManager(project_root=str(real_project_root))
        builder.set_output_style_prompt(mgr.get_current_prompt())

        first = builder._get_system_messages()

        # Change style — cache should be invalidated.
        mgr.set_current("explanatory")
        builder.set_output_style_prompt(mgr.get_current_prompt())

        second = builder._get_system_messages()
        second_l1 = second[0]["content"] if second else ""

        assert "Output Style: explanatory" in second_l1

    def test_default_prompt_is_empty_string(self, real_project_root):
        """Verify default style returns empty prompt (not None)."""
        mgr = OutputStyleManager(project_root=str(real_project_root))
        assert mgr.get_current_prompt() == ""


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case and boundary tests."""

    def test_no_output_styles_dir_does_not_crash(self):
        """Manager should work even without a prompts/output_styles/ dir."""
        tmp = tempfile.mkdtemp(prefix="output_styles_empty_")
        mgr = OutputStyleManager(project_root=tmp)
        assert mgr.get_current() == "default"
        assert "default" in mgr.list_all()

    def test_switch_back_to_default_clears_prompt(self):
        """Switching to default should return empty prompt."""
        mgr = OutputStyleManager(
            project_root=str(Path(__file__).resolve().parent.parent),
        )
        mgr.set_current("explanatory")
        assert mgr.get_current_prompt() != ""

        mgr.set_current("default")
        assert mgr.get_current_prompt() == ""

    def test_prompt_includes_style_name_header(self, real_project_root):
        mgr = OutputStyleManager(project_root=str(real_project_root))
        mgr.set_current("learning")
        prompt = mgr.get_current_prompt()
        assert prompt.startswith("\n\n# Output Style: learning")
