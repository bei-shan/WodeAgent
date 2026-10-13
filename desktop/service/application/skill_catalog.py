"""Project skill catalogue use cases, independent of FastAPI transport.

Only writable runtime skills may be edited/deleted. Built-in source skills
are reference data. Reject symlink traversal and keep writes atomic.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from threading import RLock

from core.skills.skill_loader import SkillLoader, _parse_frontmatter
from .errors import ConflictError, ForbiddenError, InvalidInputError, NotFoundError, ServiceError


_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class SkillCatalogService:
    def __init__(self, project_root: str | Path):
        self.root = Path(project_root).resolve()
        self._runtime_root = self.root / ".mycodeagent" / "skills"
        self._lock = RLock()

    @staticmethod
    def validate(name: str, description: str, content: str) -> dict:
        errors: list[str] = []
        if not _SKILL_NAME.fullmatch(name or ""):
            errors.append("name 格式无效（只允许小写字母、数字、连字符）")
        if not (description or "").strip():
            errors.append("缺少 description 字段")
        elif "\n" in description or "\r" in description:
            errors.append("description 不允许换行")
        if not (content or "").strip():
            errors.append("内容不能为空")
        return {"valid": not errors, "errors": errors}

    @staticmethod
    def _name(name: str) -> None:
        if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
            raise NotFoundError("Skill not found")

    def _loader(self) -> SkillLoader:
        return SkillLoader(str(self.root))

    def _guard(self, path: Path) -> None:
        """Verify lexical ancestors as well as resolved workspace containment."""
        try:
            relative = path.relative_to(self.root)
            cursor = self.root
            for component in relative.parts:
                cursor = cursor / component
                if cursor.is_symlink():
                    raise ForbiddenError("Skill path is a symbolic link")
            if not path.resolve().is_relative_to(self.root):
                raise ForbiddenError("Skill path escapes project root")
        except ValueError as exc:
            raise ForbiddenError("Skill path escapes project root") from exc

    def _lookup(self, name: str):
        self._name(name)
        try:
            skill = self._loader().get_skill(name, refresh=True)
        except (OSError, ValueError) as exc:
            raise ServiceError("Unable to load skills") from exc
        if skill is None:
            raise NotFoundError(f"Skill not found: {name}")
        file_path = Path(skill.path)
        self._guard(file_path)
        return skill, file_path

    def _writable(self, name: str) -> Path:
        _, file_path = self._lookup(name)
        expected = self._runtime_root / name / "SKILL.md"
        self._guard(expected)
        if file_path != expected:
            raise ForbiddenError("Built-in skills are read-only")
        return file_path

    def list(self) -> list[dict]:
        try:
            skills = self._loader().list_skills(refresh=True)
            rows = []
            for skill in skills:
                try:
                    self._guard(Path(skill.path))
                except ForbiddenError:
                    continue
                rows.append({
                    "name": skill.name,
                    "description": skill.description,
                    "base_dir": skill.base_dir,
                })
            return rows
        except (OSError, ValueError):
            return []

    def get(self, name: str) -> dict:
        skill, path = self._lookup(name)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ServiceError("Unable to read skill") from exc
        parsed = _parse_frontmatter(text)
        if parsed is None:
            raise ServiceError("Unable to parse skill")
        frontmatter, body = parsed
        return {
            "name": skill.name,
            "description": skill.description,
            "content": body.strip(),
            "frontmatter": frontmatter,
        }

    @staticmethod
    def _frontmatter(name: str, description: str, content: str) -> str:
        return (
            "---\nname: " + name + "\ndescription: " +
            json.dumps(description, ensure_ascii=False) + "\n---\n\n" +
            content.strip("\n") + "\n"
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".SKILL-", suffix=".tmp", delete=False,
            ) as stream:
                temp = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        except OSError as exc:
            raise ServiceError("Unable to write skill") from exc
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def create(self, name: str, description: str, content: str) -> dict:
        validation = self.validate(name, description, content)
        if not validation["valid"]:
            raise InvalidInputError("; ".join(validation["errors"]))
        destination = self._runtime_root / name
        target = destination / "SKILL.md"
        with self._lock:
            self._guard(target)
            try:
                existing = self._loader().get_skill(name, refresh=True)
                if existing is not None or destination.exists() or destination.is_symlink():
                    raise ConflictError(f"Skill '{name}' already exists")
                destination.mkdir(parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise ConflictError(f"Skill '{name}' already exists") from exc
            except OSError as exc:
                raise ServiceError("Unable to create skill directory") from exc
            self._atomic_write(target, self._frontmatter(name, description, content))
        return {"status": "created", "name": name}

    @staticmethod
    def _rebuild(content: str, description: str | None, body: str | None) -> str:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ServiceError("Unable to parse skill")
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            raise ServiceError("Unable to parse skill")
        front = lines[1:end]
        if description is not None:
            value = "description: " + json.dumps(description, ensure_ascii=False)
            for i, line in enumerate(front):
                if line.strip().startswith("description:"):
                    front[i] = value
                    break
            else:
                front.append(value)
        current_body = "\n".join(lines[end + 1:]) if body is None else body.strip("\n")
        return "---\n" + "\n".join(front) + "\n---\n" + current_body + "\n"

    def update(self, name: str, description: str | None, content: str | None) -> dict:
        if description is not None and (not description.strip() or "\n" in description or "\r" in description):
            raise InvalidInputError("Invalid skill description")
        with self._lock:
            file_path = self._writable(name)
            try:
                current = file_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ServiceError("Unable to read skill") from exc
            updated = self._rebuild(current, description, content)
            self._guard(file_path)
            self._atomic_write(file_path, updated)
        return {"status": "updated", "name": name}

    def delete(self, name: str) -> dict:
        with self._lock:
            file_path = self._writable(name)
            self._guard(file_path.parent)
            try:
                shutil.rmtree(file_path.parent)
            except OSError as exc:
                raise ServiceError("Unable to delete skill") from exc
        return {"status": "deleted", "name": name}
