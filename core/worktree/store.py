"""WorktreeStore — persists the worktree index to .worktrees/index.json.

Each worktree entry is a dict with:
    name, path, branch, base_ref, status, created_at, removed_at, kept_at
"""

from __future__ import annotations

import json
from pathlib import Path


class WorktreeStore:
    """Persists worktree index as .worktrees/index.json."""

    def __init__(self, worktrees_dir: Path):
        self._dir = Path(worktrees_dir)
        self._path = self._dir / "index.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_all(self) -> list[dict]:
        """Load all worktree entries, sorted by created_at."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            entries = list(data.get("worktrees", []))
        except (json.JSONDecodeError, OSError):
            return []
        entries.sort(key=lambda e: e.get("created_at", 0))
        return entries

    def find(self, name: str) -> dict | None:
        """Find a worktree entry by name. Returns None if not found."""
        for entry in self.load_all():
            if entry.get("name") == name:
                return dict(entry)
        return None

    def save(self, entry: dict) -> dict:
        """Save or update a worktree entry. Matched by name."""
        entries = self.load_all()
        found = False
        for i, existing in enumerate(entries):
            if existing.get("name") == entry.get("name"):
                entries[i] = dict(entry)
                found = True
                break
        if not found:
            entries.append(dict(entry))

        self._dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"worktrees": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return dict(entry)
