"""Deleted Python terminal must have no live imports or entrypoints."""
from __future__ import annotations
import ast
from pathlib import Path


def test_legacy_terminal_removed_and_no_live_imports():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "scripts/chat_test_agent.py", "scripts/slash_commands.py",
        "utils/ui_components.py", "tests/test_tui_streaming.py",
        "tests/test_ui_components.py",
    ):
        assert not (root / relative).exists(), "Retired UI code remains: " + relative
    assert (root / "tui" / "package.json").is_file()
    assert not (root / "tui" / "__init__.py").exists()
    excluded = {"venv", "node_modules", "dist"}
    forbidden = ("tui", "scripts.chat_test_agent", "scripts.slash_commands", "utils.ui_components")
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        # Any dot-directory (.git, .venv, .uv_cache, .pytest_cache, …) holds
        # tooling or cached third-party files, not this project's own code.
        if excluded.intersection(parts) or any(part.startswith(".") for part in parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                imports = (x.name for x in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports = (node.module or "",)
            else:
                continue
            for name in imports:
                assert not any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden), (
                    str(path) + ": old UI import " + name
                )


def test_live_docs_use_new_terminal_entrypoint():
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "docs/ARCHITECTURE.md", "tui/README.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert "python scripts/chat_test_agent.py" not in text
