#!/usr/bin/env python3
"""Check relative Markdown links in maintained Ridge Code documentation.

Historical design snapshots are excluded deliberately: old paths in archived
material are not a runtime contract.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
MAINTAINED = (
    "README.md",
    "CONTRIBUTING.md",
    "tui-v2/README.md",
    "docs/README.md",
    "docs/ARCHITECTURE.md",
    "docs/AUDIT_2026-10-13.md",
    "docs/REBRANDING.md",
    "docs/COMMANDS.md",
    "docs/REPOSITORY_INVENTORY.md",
)
LINK = re.compile(r"(?<!!)\]\(([^)\n]+)\)")


def broken_links(relative_path: str) -> list[str]:
    source = ROOT / relative_path
    errors: list[str] = []
    if not source.is_file():
        return [f"{relative_path}: source file missing"]
    in_fence = False
    for lineno, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK.finditer(line):
            raw = match.group(1).strip().strip("<>")
            if raw.startswith("#") or raw.startswith(("mailto:", "data:")):
                continue
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc or raw.startswith("//"):
                continue
            path = unquote(parsed.path)
            if not path:
                continue
            target = (source.parent / path).resolve()
            if not target.is_relative_to(ROOT) or not target.exists():
                errors.append(f"{relative_path}:{lineno}: broken link {raw}")
    return errors


def main() -> int:
    errors = [msg for name in MAINTAINED for msg in broken_links(name)]
    if errors:
        print("\n".join(errors))
        return 1
    print(f"OK: {len(MAINTAINED)} maintained Markdown files have valid local links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
