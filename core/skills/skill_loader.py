"""Skill loader — two-layer design (like hermes-agent).

skills/                    Source directory (built-in defaults, tracked by git)
.mycodeagent/skills/       Runtime directory (auto-created, user/agent writes here)

On first use, built-in skills are copied from skills/ → .mycodeagent/skills/.
The agent reads/writes from the runtime directory.  The source directory is
read-only reference — user deletions in runtime don't affect the defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class SkillMeta:
    name: str
    description: str
    path: str
    base_dir: str
    mtime: float


class SkillLoader:
    """Scan and cache skills.

    Source dir:   skills/<name>/SKILL.md              (built-in, git-tracked)
    Runtime dir:  .mycodeagent/skills/<name>/SKILL.md  (auto-created, writable)
    """

    # ── Paths ───────────────────────────────────────────────────────
    SOURCE_DIR = "skills"
    RUNTIME_DIR = ".mycodeagent/skills"

    def __init__(self, project_root: str):
        self._project_root = Path(project_root).resolve()
        self._source_dir = self._project_root / self.SOURCE_DIR
        self._runtime_dir = self._project_root / self.RUNTIME_DIR
        self._skills: Dict[str, SkillMeta] = {}
        self._last_scan_mtime: float = 0.0
        self._last_scan_count: int = 0

    # ── Public API ──────────────────────────────────────────────────

    def scan(self) -> List[SkillMeta]:
        """Scan skill directories and refresh cache.

        Auto-creates the runtime directory and copies built-in defaults
        from source on first use.
        """
        self._ensure_runtime_dir()
        self._seed_defaults_if_empty()

        skills: Dict[str, SkillMeta] = {}
        max_mtime = 0.0
        count = 0

        for path in self._iter_skill_files():
            count += 1
            try:
                stat = path.stat()
                max_mtime = max(max_mtime, stat.st_mtime)
            except OSError:
                continue

            meta = self._parse_skill_file(path)
            if not meta:
                continue

            # Runtime entries override source entries with same name.
            existing = skills.get(meta.name)
            if existing is not None:
                if str(self._runtime_dir) in str(meta.path):
                    skills[meta.name] = meta  # runtime wins
                continue
            skills[meta.name] = meta

        self._skills = skills
        self._last_scan_mtime = max_mtime
        self._last_scan_count = count
        return self.list_skills(refresh=False)

    def refresh_if_stale(self) -> List[SkillMeta]:
        """Refresh cache if skill files changed."""
        if not self._skills:
            return self.scan()
        current_max_mtime, current_count = self._get_skills_state()
        if current_max_mtime != self._last_scan_mtime or current_count != self._last_scan_count:
            return self.scan()
        return self.list_skills(refresh=False)

    def list_skills(self, refresh: bool = False) -> List[SkillMeta]:
        if refresh:
            self.refresh_if_stale()
        return sorted(self._skills.values(), key=lambda s: s.name)

    def get_skill(self, name: str, refresh: bool = False) -> Optional[SkillMeta]:
        if refresh:
            self.refresh_if_stale()
        return self._skills.get(name)

    def format_skills_for_prompt(self, char_budget: int) -> str:
        skills = self.list_skills(refresh=False)
        if not skills:
            return "(none)"
        lines: List[str] = []
        used = 0
        for skill in skills:
            line = f"- {skill.name}: {skill.description}"
            line_len = len(line) + 1
            if used + line_len > char_budget and lines:
                break
            if used + line_len > char_budget and not lines:
                break
            lines.append(line)
            used += line_len
        return "\n".join(lines) if lines else "(none)"

    # ── Internal ────────────────────────────────────────────────────

    def _ensure_runtime_dir(self) -> None:
        if not self._runtime_dir.exists():
            self._runtime_dir.mkdir(parents=True, exist_ok=True)

    def _seed_defaults_if_empty(self) -> None:
        """Copy built-in skills from source dir to runtime dir on first use."""
        if not self._source_dir.exists():
            return
        # Only seed if runtime dir has no skills at all.
        if any(self._runtime_dir.rglob("SKILL.md")):
            return
        for src_path in sorted(self._source_dir.rglob("SKILL.md")):
            try:
                rel = src_path.relative_to(self._source_dir)
                dst = self._runtime_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst)
            except OSError:
                pass

    def _iter_skill_files(self) -> List[Path]:
        """Scan runtime dir (primary) and source dir (fallback)."""
        paths: List[Path] = []
        # Runtime dir — primary (user/agent writes go here)
        if self._runtime_dir.exists():
            paths.extend(self._runtime_dir.rglob("SKILL.md"))
        # Source dir — built-in defaults (reference)
        if self._source_dir.exists():
            paths.extend(self._source_dir.rglob("SKILL.md"))
        return sorted(paths)

    def _get_skills_state(self) -> Tuple[float, int]:
        max_mtime = 0.0
        count = 0
        for path in self._iter_skill_files():
            count += 1
            try:
                stat = path.stat()
            except OSError:
                continue
            max_mtime = max(max_mtime, stat.st_mtime)
        return max_mtime, count

    def _parse_skill_file(self, path: Path) -> Optional[SkillMeta]:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None

        parsed = _parse_frontmatter(content)
        if not parsed:
            return None

        frontmatter, _body = parsed
        name = (frontmatter.get("name") or "").strip()
        description = (frontmatter.get("description") or "").strip()

        if not name or not description:
            return None
        if not _SKILL_NAME_PATTERN.match(name):
            return None

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

        try:
            base_dir = str(path.parent.relative_to(self._project_root)) or "."
        except ValueError:
            base_dir = str(path.parent)
        return SkillMeta(
            name=name,
            description=description,
            path=str(path),
            base_dir=base_dir,
            mtime=mtime,
        )


def _parse_frontmatter(content: str) -> Optional[Tuple[Dict[str, str], str]]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return None

    frontmatter_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])
    frontmatter: Dict[str, str] = {}

    for line in frontmatter_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            return None
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            return None
        frontmatter[key] = value

    return frontmatter, body
