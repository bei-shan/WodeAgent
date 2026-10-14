"""Skill loader — two-layer design (like hermes-agent).

skills/                    Source directory (built-in defaults, tracked by git)
.mycodeagent/skills/       Runtime directory (auto-created, user/agent writes here)

On first use, built-in skills are copied from skills/ → .mycodeagent/skills/.
The agent reads/writes from the runtime directory.  The source directory is
read-only reference — user deletions in runtime don't affect the defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
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
        disabled = self._disabled_skill_names()
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
            if not meta or meta.name in disabled:
                continue

            # Runtime entries override source entries with same name.
            existing = skills.get(meta.name)
            if existing is not None:
                if path.is_relative_to(self._runtime_dir):
                    skills[meta.name] = meta  # explicit user runtime override
                elif not Path(existing.path).is_relative_to(self._runtime_dir):
                    if Path(existing.path).is_relative_to(self._source_dir):
                        continue  # built-in source takes precedence over plugins
                    if path.is_relative_to(self._source_dir):
                        skills[meta.name] = meta
                # Otherwise the first deterministic plugin wins
                continue
            skills[meta.name] = meta

        self._skills = skills
        self._last_scan_mtime = max_mtime
        self._last_scan_count = count
        return self.list_skills(refresh=False)

    def refresh_if_stale(self) -> List[SkillMeta]:
        """Refresh on skill edits and plugin enable/disable state changes."""
        from core.plugin_loader import PluginCatalog
        if PluginCatalog(self._project_root).manifests() or (
            self._project_root / "skill_state.json"
        ).exists():
            # Lightweight local metadata scan: plugin state is independent of
            # SKILL.md timestamps and must invalidate prompt discovery.
            return self.scan()
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

    def create_skill(self, name: str, description: str, content: str) -> Path:
        """Atomically create a project-runtime skill, independent of tool CWD."""
        import os
        import tempfile
        if not isinstance(name, str) or not _SKILL_NAME_PATTERN.fullmatch(name):
            raise ValueError("Invalid skill name")
        if (not isinstance(description, str) or not description.strip()
                or "\n" in description or "\r" in description):
            raise ValueError("Invalid skill description")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Skill content is required")
        if self.get_skill(name, refresh=True):
            raise FileExistsError("Skill already exists")
        target = self._runtime_dir / name / "SKILL.md"
        if not self._safe_skill_path(target):
            raise ValueError("Unsafe skill destination")
        target.parent.mkdir(parents=True, exist_ok=False)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent,
                prefix=".skill-", suffix=".tmp", delete=False,
            ) as handle:
                temp = Path(handle.name)
                handle.write("---\nname: " + name + "\ndescription: " +
                             json.dumps(description, ensure_ascii=False) +
                             "\n---\n\n" + content.strip() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not self._safe_skill_path(target):
                raise ValueError("Unsafe skill destination")
            os.replace(temp, target)
            temp = None
        except Exception:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if not target.exists():
                target.parent.rmdir()
            raise
        self.scan()
        return target

    # ── Internal ────────────────────────────────────────────────────

    def _disabled_skill_names(self) -> set[str]:
        """Honor the existing Skills enable/disable UI state in the Agent."""
        path = self._project_root / "skill_state.json"
        if not self._safe_skill_path(path):
            return set()
        try:
            if path.stat().st_size > 128 * 1024:
                return set()
            data = json.loads(path.read_text(encoding="utf-8"))
            disabled = data.get("disabled_skills", {}) if isinstance(data, dict) else {}
            return {name for name, value in disabled.items()
                    if isinstance(name, str) and value is True} if isinstance(disabled, dict) else set()
        except (OSError, ValueError, TypeError):
            return set()

    def _safe_skill_path(self, path: Path) -> bool:
        """Never follow symlinked skill entries or project-external paths."""
        try:
            relative = path.relative_to(self._project_root)
            current = self._project_root
            for component in relative.parts:
                current = current / component
                if current.is_symlink():
                    return False
            return path.resolve().is_relative_to(self._project_root)
        except (OSError, ValueError, RuntimeError):
            return False

    def _ensure_runtime_dir(self) -> None:
        if not self._safe_skill_path(self._runtime_dir):
            raise ValueError("Unsafe skill runtime directory")
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
                if not self._safe_skill_path(src_path) or not self._safe_skill_path(dst):
                    continue
                # Copy complete supporting assets, not just SKILL.md. A
                # skill with scripts/references must remain functional after
                # runtime seeding. Never follow source symlinks.
                source_skill = src_path.parent
                target_skill = dst.parent
                for asset in sorted(source_skill.rglob("*")):
                    if not self._safe_skill_path(asset):
                        continue
                    relative_asset = asset.relative_to(source_skill)
                    destination = target_skill / relative_asset
                    if not self._safe_skill_path(destination):
                        continue
                    if asset.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    elif asset.is_file() and not destination.exists():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(asset, destination)
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
        # Enabled plugin skill trees are read in-place; they are never copied
        # into writable runtime Skills, so uninstall/revoke takes effect.
        from core.plugin_loader import PluginCatalog
        for root in PluginCatalog(self._project_root).active_skill_dirs():
            paths.extend(root.rglob("SKILL.md"))
        return sorted(p for p in paths if self._safe_skill_path(p))

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
        if not self._safe_skill_path(path):
            return None
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
