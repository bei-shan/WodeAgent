"""Generate AGENTS.md from a repository, opencode ``/init`` style.

Deliberately deterministic: it reads manifests, scripts and layout, never calls
a model. output is a starting point a human edits, with explicit TODO markers
where a heuristic cannot decide.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", ".next", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".mycodeagent", "memory", "tool-output", ".idea", ".vscode",
}
LANGUAGE_BY_SUFFIX = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
    ".css": "CSS", ".md": "Markdown", ".json": "JSON", ".yml": "YAML", ".yaml": "YAML",
}
MAX_WALK_FILES = 4000
MAX_LISTED_DIRS = 10


def _walk(root: Path, limit: int = MAX_WALK_FILES):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in IGNORED_DIRS and not name.startswith(".")
        )
        for name in filenames:
            if name.startswith("."):
                continue
            yield Path(dirpath) / name
            limit -= 1
            if limit <= 0:
                return


def _package_scripts(root: Path) -> dict[str, str]:
    package = root / "package.json"
    if not package.is_file():
        return {}
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


def _make_targets(root: Path) -> list[str]:
    makefile = root / "Makefile"
    if not makefile.is_file():
        return []
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", text, flags=re.MULTILINE)[:12]


def scan_project(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    languages: Counter[str] = Counter()
    for path in _walk(root):
        language = LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if language:
            languages[language] += 1

    scripts = _package_scripts(root)
    commands: list[tuple[str, str]] = []
    if scripts.get("test"):
        commands.append(("测试", f"npm test"))
    if scripts.get("build"):
        commands.append(("构建", "npm run build"))
    if scripts.get("lint"):
        commands.append(("检查", "npm run lint"))
    if scripts.get("dev"):
        commands.append(("开发", "npm run dev"))

    python_project = any(
        (root / name).exists()
        for name in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")
    )
    if python_project:
        tests_dir = root / "tests"
        commands.append((
            "测试",
            "python -m pytest tests/ -q" if tests_dir.is_dir() else "python -m pytest -q",
        ))
    if (root / "ridgecode").is_dir():
        commands.append(("Agent 入口", "python -m ridgecode --help"))
    for target in _make_targets(root):
        commands.append((f"make {target}", f"make {target}"))

    layout: list[tuple[str, int]] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or child.name in IGNORED_DIRS or child.name.startswith("."):
            continue
        count = sum(1 for _ in child.rglob("*") if _.is_file())
        layout.append((child.name, count))
    layout.sort(key=lambda item: item[1], reverse=True)

    existing = [
        name for name in ("AGENTS.md", "CLAUDE.md", "CODE_LAW.md")
        if (root / name).is_file()
    ]
    return {
        "root": str(root),
        "languages": languages.most_common(5),
        "commands": _dedupe(commands),
        "layout": layout[:MAX_LISTED_DIRS],
        "existing_instructions": existing,
        "has_readme": (root / "README.md").is_file(),
    }


def _dedupe(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for label, command in items:
        key = command.strip()
        if key in seen:
            continue
        seen.add(key)
        result.append((label, key))
    return result


def render_agents_md(scan: dict[str, Any]) -> str:
    lines = [
        "# AGENTS.md",
        "",
        "> 由 `python -m ridgecode init` 扫描生成；请按需修改并提交到仓库。",
        "> 每轮对话都会自动注入，保持简短。",
        "",
        "## 项目概览",
        "",
    ]
    if scan["languages"]:
        summary = "、".join(f"{name}（{count} 文件）" for name, count in scan["languages"])
        lines.append(f"- 主要语言：{summary}")
    else:
        lines.append("- 主要语言：<!-- TODO -->")
    lines.append("- 入口：<!-- TODO: 例如 python -m ridgecode 或 npm run dev -->")
    lines.append("")

    lines.append("## 常用命令")
    lines.append("")
    if scan["commands"]:
        for label, command in scan["commands"]:
            lines.append(f"- {label}：`{command}`")
    else:
        lines.append("- <!-- TODO: 测试 / 构建 / 检查命令 -->")
    lines.append("")

    lines.append("## 目录结构")
    lines.append("")
    if scan["layout"]:
        for name, count in scan["layout"]:
            lines.append(f"- `{name}/` —— {count} 个文件")
    else:
        lines.append("- <!-- TODO: 说明核心目录 -->")
    lines.append("")

    lines.append("## 约定")
    lines.append("")
    lines.append("- 提交前运行测试；不要提交 `.env`、密钥或本地数据目录。")
    lines.append("- <!-- TODO: 代码风格、分支策略、评审要求 -->")
    lines.append("")

    if scan["existing_instructions"]:
        lines.append("## 已有规则文件")
        lines.append("")
        for name in scan["existing_instructions"]:
            lines.append(f"- `{name}` —— 会自动注入，避免与本文重复。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_agents_md(
    root: str | Path, content: str | None = None, *, force: bool = False,
) -> Path:
    root = Path(root)
    target = root / "AGENTS.md"
    if target.exists() and not force:
        raise FileExistsError(str(target))
    text = content if content is not None else render_agents_md(scan_project(root))
    target.write_text(text, encoding="utf-8")
    return target
