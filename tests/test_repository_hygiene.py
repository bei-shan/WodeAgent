"""Repository hygiene invariants for the current, non-legacy architecture."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_retired_implementations_and_archived_mvp_documents_are_absent():
    for relative in (
        "core/features",
        "docs/archive",
        "docs/design/2026-06-29-feature-system-reflections.md",
        "__init__.py",
    ):
        assert not (ROOT / relative).exists(), f"Retired file reappeared: {relative}"


def test_current_architecture_and_readme_use_live_paths_and_one_command():
    architecture = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = (ROOT / "docs/COMMANDS.md").read_text(encoding="utf-8")
    assert (ROOT / "ridgecode/__main__.py").is_file()
    assert "python -m ridgecode" in architecture
    assert "python -m ridgecode setup" in readme
    assert "python -m ridgecode serve" in readme
    assert "python -m ridgecode tui" in commands
    assert "`core/features/`、" in architecture  # explicit retirement notice only
    assert "WODEAGENT_SERVER" not in readme
    assert "docs/archive" not in readme


def test_runtime_state_files_never_show_up_as_untracked_changes():
    """The Skills/MCP toggles write runtime state into the project root, which
    must stay out of `git status`."""
    ignored = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert "skill_state.json" in ignored


def test_tui_entrypoint_never_resurrects_old_environment_aliases():
    main = (ROOT / "tui-v2/src/index.tsx").read_text(encoding="utf-8")
    assert "RIDGECODE_SERVER" in main
    assert "RIDGECODE_AUTH_TOKEN" in main
    assert "WODEAGENT_SERVER" not in main
    assert "WODEAGENT_AUTH_TOKEN" not in main
