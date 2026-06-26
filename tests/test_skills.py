"""Skill system tests."""

from core.skills.skill_loader import SkillLoader
from tools.builtin.skill import SkillTool
from tests.utils.test_helpers import parse_response


def test_skill_loader_scans_skills(temp_project):
    temp_project.create_file(
        "skills/code-review/SKILL.md",
        """---
name: code-review
description: Review code quality
---
# Code Review
""",
    )

    loader = SkillLoader(str(temp_project.root))
    skills = loader.scan()

    assert len(skills) == 1
    assert skills[0].name == "code-review"
    assert skills[0].description == "Review code quality"


def test_skill_loader_skips_invalid_frontmatter(temp_project):
    temp_project.create_file(
        "skills/bad/SKILL.md",
        """# Missing frontmatter
content
""",
    )

    loader = SkillLoader(str(temp_project.root))
    skills = loader.scan()

    assert skills == []


def test_skill_tool_loads_and_expands_arguments(temp_project):
    temp_project.create_file(
        "skills/code-review/SKILL.md",
        """---
name: code-review
description: Review code quality
---
# Code Review

Check this file:

$ARGUMENTS
""",
    )

    loader = SkillLoader(str(temp_project.root))
    loader.scan()

    tool = SkillTool(project_root=temp_project.root, skill_loader=loader)
    response = tool.run({"name": "code-review", "args": "src/main.py"})
    parsed = parse_response(response)

    assert parsed["status"] == "success"
    data = parsed["data"]
    assert data["name"] == "code-review"
    assert "Base directory for this skill" in data["content"]
    assert "src/main.py" in data["content"]


# ---------------------------------------------------------------------------
# Extended tests
# ---------------------------------------------------------------------------


def test_format_skills_for_prompt_basic(temp_project):
    """format_skills_for_prompt returns a formatted list."""
    temp_project.create_file(
        "skills/code-review/SKILL.md",
        """---
name: code-review
description: Review code quality
---
# Code Review
""",
    )

    loader = SkillLoader(str(temp_project.root))
    loader.scan()
    result = loader.format_skills_for_prompt(char_budget=2000)

    assert "code-review" in result
    assert "Review code quality" in result


def test_format_skills_for_prompt_empty(temp_project):
    """format_skills_for_prompt returns '(none)' when no skills exist."""
    loader = SkillLoader(str(temp_project.root))
    loader.scan()
    result = loader.format_skills_for_prompt(char_budget=2000)

    assert result == "(none)"


def test_format_skills_for_prompt_truncation(temp_project):
    """format_skills_for_prompt truncates when char_budget is small."""
    temp_project.create_file(
        "skills/code-review/SKILL.md",
        """---
name: code-review
description: Review code quality
---
# Code Review
""",
    )
    temp_project.create_file(
        "skills/ui-design/SKILL.md",
        """---
name: ui-design
description: Design UI components
---
# UI Design
""",
    )

    loader = SkillLoader(str(temp_project.root))
    loader.scan()

    # With a tiny budget, only the first skill should appear
    result = loader.format_skills_for_prompt(char_budget=20)
    # Should contain at most one skill (budget too small for both)
    lines = [l for l in result.split("\n") if l.startswith("- ")]
    assert len(lines) <= 1


def test_refresh_if_stale_detects_new_file(temp_project):
    """refresh_if_stale re-scans when a new SKILL.md appears."""
    loader = SkillLoader(str(temp_project.root))
    loader.scan()
    assert len(loader.list_skills()) == 0

    temp_project.create_file(
        "skills/lint/SKILL.md",
        """---
name: lint
description: Lint code
---
# Lint
""",
    )

    loader.refresh_if_stale()
    skills = loader.list_skills()
    assert len(skills) == 1
    assert skills[0].name == "lint"


def test_refresh_if_stale_detects_modified_file(temp_project):
    """refresh_if_stale re-scans when a SKILL.md is modified."""
    import time

    skill_path = temp_project.create_file(
        "skills/lint/SKILL.md",
        """---
name: lint
description: Lint code
---
# Lint
""",
    )

    loader = SkillLoader(str(temp_project.root))
    loader.scan()
    assert len(loader.list_skills()) == 1

    # Ensure mtime changes (some filesystems have 1-2s resolution)
    time.sleep(0.1)

    # Modify the runtime copy (source is read-only reference)
    runtime_path = temp_project.root / ".mycodeagent" / "skills" / "lint" / "SKILL.md"
    runtime_path.write_text(
        """---
name: lint
description: Lint and format code
---
# Lint
""",
        encoding="utf-8",
    )

    loader.refresh_if_stale()
    skills = loader.list_skills()
    assert len(skills) == 1
    assert "format" in skills[0].description


def test_skill_name_validation_rejects_invalid(temp_project):
    """Skills with names that don't match kebab-case pattern are rejected."""
    temp_project.create_file(
        "skills/My Skill/SKILL.md",
        """---
name: My Skill
description: Has spaces
---
# Invalid
""",
    )

    loader = SkillLoader(str(temp_project.root))
    loader.scan()
    assert len(loader.list_skills()) == 0


def test_skill_missing_arguments_expands_to_end(temp_project):
    """When $ARGUMENTS is not in the skill body, args are appended."""
    temp_project.create_file(
        "skills/simple/SKILL.md",
        """---
name: simple
description: A simple skill
---
# Simple

No arguments placeholder here.
""",
    )

    loader = SkillLoader(str(temp_project.root))
    loader.scan()

    tool = SkillTool(project_root=temp_project.root, skill_loader=loader)
    response = tool.run({"name": "simple", "args": "file.txt"})
    parsed = parse_response(response)

    assert parsed["status"] == "success"
    assert "ARGUMENTS" in parsed["data"]["content"]
    assert "file.txt" in parsed["data"]["content"]


def test_skill_empty_frontmatter_rejected(temp_project):
    """A SKILL.md with empty frontmatter is rejected."""
    temp_project.create_file(
        "skills/empty/SKILL.md",
        """---
---
# No frontmatter fields
""",
    )

    loader = SkillLoader(str(temp_project.root))
    loader.scan()
    assert len(loader.list_skills()) == 0


def test_skill_frontmatter_missing_description_rejected(temp_project):
    """A SKILL.md with name but no description is rejected."""
    temp_project.create_file(
        "skills/nodesc/SKILL.md",
        """---
name: nodesc
---
# Missing description
""",
    )

    loader = SkillLoader(str(temp_project.root))
    loader.scan()
    assert len(loader.list_skills()) == 0
