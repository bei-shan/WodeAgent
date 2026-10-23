"""Instruction files (AGENTS.md / CLAUDE.md / CODE_LAW.md) and `ridgecode init`."""
from __future__ import annotations

from core.context_engine.instruction_files import (
    find_instruction_files,
    instruction_signature,
    load_instructions,
)
from core.instructions_generator import render_agents_md, scan_project, write_agents_md


def test_loads_all_three_files_with_labels(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Use pnpm, not npm.", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Answer in Chinese.", encoding="utf-8")
    (tmp_path / "CODE_LAW.md").write_text("Legacy rule.", encoding="utf-8")

    names = [path.name for path, _label in find_instruction_files(tmp_path)]
    assert names == ["AGENTS.md", "CLAUDE.md", "CODE_LAW.md"]

    text = load_instructions(tmp_path)
    assert "AGENTS.md — project instructions" in text
    assert "CLAUDE.md — Claude Code compatible instructions" in text
    assert "CODE_LAW.md — legacy Ridge Code workspace rules" in text
    assert text.index("Use pnpm") < text.index("Answer in Chinese")


def test_missing_files_yield_empty_text(tmp_path):
    assert load_instructions(tmp_path) == ""
    assert instruction_signature(tmp_path) == ()


def test_lowercase_alias_is_not_loaded_twice(tmp_path):
    # On case-insensitive filesystems AGENTS.md and agents.md are one file.
    (tmp_path / "AGENTS.md").write_text("rule", encoding="utf-8")
    assert len(find_instruction_files(tmp_path)) == 1


def test_size_caps_apply_per_file_and_overall(tmp_path):
    (tmp_path / "AGENTS.md").write_text("a" * 50_000, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("b" * 50_000, encoding="utf-8")
    text = load_instructions(tmp_path)
    assert len(text) <= 20_000 + len("\n[truncated]")
    assert "[truncated]" in text


def test_signature_changes_when_a_file_changes(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("one", encoding="utf-8")
    first = instruction_signature(tmp_path)
    agents.write_text("two bytes", encoding="utf-8")
    assert instruction_signature(tmp_path) != first


def test_context_builder_injects_instructions(tmp_path):
    from core.context_engine.context_builder import ContextBuilder
    from tools.registry import ToolRegistry

    (tmp_path / "AGENTS.md").write_text("Always run pytest before pushing.", encoding="utf-8")
    builder = ContextBuilder(tool_registry=ToolRegistry(), project_root=str(tmp_path))
    refs = builder.get_reference_messages()
    assert any("pytest before pushing" in str(block.get("content", "")) for block in refs)
    assert any(
        "workspace instructions" in str(block.get("content", "")) for block in refs
    )


def test_generator_writes_agents_md_with_detected_commands(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    scan = scan_project(tmp_path)
    assert ("Python", 2) in scan["languages"]
    assert any(command == "python -m pytest tests/ -q" for _label, command in scan["commands"])

    content = render_agents_md(scan)
    assert "# AGENTS.md" in content
    assert "python -m pytest tests/ -q" in content
    assert "`core/`" in content

    path = write_agents_md(tmp_path, content)
    assert path.name == "AGENTS.md"
    assert path.read_text(encoding="utf-8") == content

    try:
        write_agents_md(tmp_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected FileExistsError without force")


def test_generator_reads_package_scripts_and_makefile(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest", "build": "vite build"}}', encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text("test:\n\techo hi\n", encoding="utf-8")

    scan = scan_project(tmp_path)
    commands = [command for _label, command in scan["commands"]]
    assert "npm test" in commands
    assert "npm run build" in commands
    assert "make test" in commands


def test_cli_init_writes_and_refuses_to_overwrite(tmp_path, capsys, monkeypatch):
    from ridgecode.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert (tmp_path / "AGENTS.md").is_file()
    assert "Wrote" in capsys.readouterr().out

    assert main(["init"]) == 1
    assert "already exists" in capsys.readouterr().err

    assert main(["init", "--force"]) == 0


def test_cli_init_stdout_changes_nothing(tmp_path, capsys, monkeypatch):
    from ridgecode.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["init", "--stdout"]) == 0
    output = capsys.readouterr().out
    assert "# AGENTS.md" in output
    assert not (tmp_path / "AGENTS.md").exists()
