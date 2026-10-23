"""Workspace instruction files, shared by Claude Code, opencode and Ridge Code.

Claude Code loads ``CLAUDE.md``; opencode loads ``AGENTS.md`` (and reads
``CLAUDE.md`` for compatibility). Ridge Code historically used ``CODE_LAW.md``.
All three are the same idea — human-owned instructions injected every turn — so
they share one loader with explicit labels. ``CODE_LAW.md`` is kept as a legacy
alias rather than a requirement: new projects should use ``AGENTS.md``.
"""
from __future__ import annotations

from pathlib import Path

INSTRUCTION_FILES = (
    ("AGENTS.md", "AGENTS.md — project instructions"),
    ("CLAUDE.md", "CLAUDE.md — Claude Code compatible instructions"),
    ("CODE_LAW.md", "CODE_LAW.md — legacy Ridge Code workspace rules"),
)
MAX_FILE_CHARS = 12_000
MAX_TOTAL_CHARS = 20_000


def find_instruction_files(project_root: str | Path) -> list[tuple[Path, str]]:
    """Existing instruction files in precedence order, case-insensitive."""
    root = Path(project_root)
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for filename, label in INSTRUCTION_FILES:
        for candidate in (root / filename, root / filename.lower()):
            if candidate.name in seen:
                continue
            try:
                exists = candidate.is_file()
            except OSError:
                continue
            if not exists:
                continue
            seen.add(candidate.name)
            found.append((candidate, label))
            break
    return found


def load_instructions(
    project_root: str | Path, *, max_chars: int = MAX_TOTAL_CHARS,
) -> str:
    """Combined instruction text; empty when no file exists.

    Every file is capped, and the combined text is capped again, so a runaway
    rules file can never crowd out the real conversation.
    """
    parts: list[str] = []
    for path, label in find_instruction_files(project_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n[truncated]"
        parts.append(f"### {label}\n{text}")
    if not parts:
        return ""
    combined = "\n\n".join(parts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n[truncated]"
    return combined


def instruction_signature(project_root: str | Path) -> tuple:
    """Cheap cache key: (name, mtime, size) for every instruction file."""
    signature: list[tuple[str, int, int]] = []
    for path, _label in find_instruction_files(project_root):
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(signature)
