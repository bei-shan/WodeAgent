"""Skill tool - loads skill instructions from project-local skills."""

import os
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from core.skills.skill_loader import SkillLoader, _parse_frontmatter
from ..base import Tool, ToolParameter, ErrorCode
from core.env import load_env

load_env()


class SkillTool(Tool):
    """Load a skill by name and return its expanded content."""
    usage_notes = 'Skill: Load a named skill by its identifier. Use when user mentions a skill by name (e.g. $code-review) or when task clearly matches a skill description. Do not preload all skills.'

    def __init__(
        self,
        name: str = "Skill",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        skill_loader: Optional[SkillLoader] = None,
    ):
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")

        super().__init__(
            name=name,
            description='Loads a skill (structured instructions) from the local skills directory.',
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )

        self._skill_loader = skill_loader or SkillLoader(str(self._project_root))

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="'load' (default), 'read_resource' to read a skill reference, 'create', or 'list'",
                required=False,
                default="load",
            ),
            ToolParameter(
                name="name",
                type="string",
                description="Skill name (required for load/create)",
                required=True,
            ),
            ToolParameter(
                name="resource",
                type="string",
                description="Relative resource path for read_resource (e.g. references/checklist.md)",
                required=False,
                default="",
            ),
            ToolParameter(
                name="args",
                type="string",
                description="Optional arguments for the skill (load action)",
                required=False,
                default="",
            ),
            ToolParameter(
                name="description",
                type="string",
                description="Skill description — required when creating",
                required=False,
                default="",
            ),
            ToolParameter(
                name="content",
                type="string",
                description="Skill body in markdown — required when creating",
                required=False,
                default="",
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        start_time = time.monotonic()
        params_input = dict(parameters)

        action = (parameters.get("action") or "load").strip().lower()
        name = parameters.get("name")
        args = parameters.get("args") or ""

        if action == "list":
            skills = self._skill_loader.list_skills(refresh=True)
            lines = [f"- {s.name}: {s.description}" for s in skills]
            return self.create_success_response(
                data={"skills": [{"name": s.name, "description": s.description} for s in skills]},
                text="Available skills:\n" + ("\n".join(lines) if lines else "(none)"),
                params_input=params_input,
            )

        if action == "create":
            if not isinstance(name, str) or not name.strip():
                return self.create_error_response(ErrorCode.INVALID_PARAM, "Parameter 'name' is required.", params_input=params_input)
            description = (parameters.get("description") or "").strip()
            content = (parameters.get("content") or "").strip()
            if not description:
                return self.create_error_response(ErrorCode.INVALID_PARAM, "Parameter 'description' is required when creating a skill.", params_input=params_input)
            if not content:
                return self.create_error_response(ErrorCode.INVALID_PARAM, "Parameter 'content' is required when creating a skill.", params_input=params_input)

            # Check name format
            import re
            if not re.match(r'^[a-z0-9]+(?:-[a-z0-9]+)*$', name.strip()):
                return self.create_error_response(ErrorCode.INVALID_PARAM, "Skill name must be lowercase letters, digits, and hyphens only.", params_input=params_input)

            # Resolve writable skill storage through the shared catalog, not
            # the isolated Web/Team tool execution workspace.
            try:
                target = self._skill_loader.create_skill(name.strip(), description, content)
                return self.create_success_response(
                    data={"name": name.strip(), "path": str(target.parent)},
                    text=f"Skill '{name}' created at {target.parent}",
                    params_input=params_input,
                )
            except FileExistsError:
                return self.create_error_response(
                    ErrorCode.CONFLICT, f"Skill '{name}' already exists.",
                    params_input=params_input,
                )
            except ValueError as exc:
                return self.create_error_response(
                    ErrorCode.INVALID_PARAM, str(exc), params_input=params_input,
                )
            except OSError:
                return self.create_error_response(
                    ErrorCode.INTERNAL_ERROR, "Failed to create skill.",
                    params_input=params_input,
                )

        if action == "read_resource":
            # Resource reads are scoped to a single *enabled* skill's own
            # directory. They do not widen the sandbox of the Read tool.
            resource = parameters.get("resource")
            if not isinstance(name, str) or not name.strip():
                return self.create_error_response(
                    ErrorCode.INVALID_PARAM, "Skill name is required.",
                    params_input=params_input,
                )
            if not isinstance(resource, str) or not resource:
                return self.create_error_response(
                    ErrorCode.INVALID_PARAM, "Relative resource path is required.",
                    params_input=params_input,
                )
            rel = PurePosixPath(resource)
            if (rel.is_absolute() or ".." in rel.parts or "\\\\" in resource
                    or len(rel.parts) > 8 or resource.startswith(".")):
                return self.create_error_response(
                    ErrorCode.PERMISSION_DENIED, "Unsafe skill resource path.",
                    params_input=params_input,
                )
            meta = self._skill_loader.get_skill(name.strip(), refresh=True)
            if meta is None:
                return self.create_error_response(
                    ErrorCode.NOT_FOUND, "Skill is not available.",
                    params_input=params_input,
                )
            base = Path(meta.path).parent
            target = base.joinpath(*rel.parts)
            if (not self._skill_loader._safe_skill_path(target)
                    or not target.resolve().is_relative_to(base.resolve())):
                return self.create_error_response(
                    ErrorCode.PERMISSION_DENIED, "Skill resource escapes its directory.",
                    params_input=params_input,
                )
            if target.suffix.lower() not in {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".py", ".ts"}:
                return self.create_error_response(
                    ErrorCode.INVALID_PARAM, "Unsupported skill resource type.",
                    params_input=params_input,
                )
            try:
                if not target.is_file():
                    raise FileNotFoundError()
                if target.stat().st_size > 128 * 1024:
                    return self.create_error_response(
                        ErrorCode.INVALID_PARAM, "Skill resource exceeds 128 KiB.",
                        params_input=params_input,
                    )
                data = target.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return self.create_error_response(
                    ErrorCode.NOT_FOUND, "Skill resource not found or unreadable.",
                    params_input=params_input,
                )
            return self.create_success_response(
                data={"name": meta.name, "resource": resource, "content": data},
                text=f"Loaded resource for skill '{meta.name}'.",
                params_input=params_input,
            )

        # Default: load
        if not isinstance(name, str) or not name.strip():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'name' is required and must be a non-empty string.",
                params_input=params_input,
            )

        refresh = _env_flag("SKILLS_REFRESH_ON_CALL", default=True)
        skill_meta = self._skill_loader.get_skill(name.strip(), refresh=refresh)
        if not skill_meta and not refresh:
            skill_meta = self._skill_loader.get_skill(name.strip(), refresh=True)
        if not skill_meta:
            # Show available skills in error
            available = [s.name for s in self._skill_loader.list_skills()]
            hint = f" Available: {', '.join(available)}" if available else " No skills installed."
            return self.create_error_response(
                error_code=ErrorCode.NOT_FOUND,
                message=f"Skill '{name}' not found.{hint}",
                params_input=params_input,
            )

        skill_path = Path(skill_meta.path)
        try:
            rel_path = str(skill_path.relative_to(self._project_root))
        except ValueError:
            rel_path = str(skill_path)
        try:
            raw_content = skill_path.read_text(encoding="utf-8")
        except PermissionError:
            return self.create_error_response(
                error_code=ErrorCode.PERMISSION_DENIED,
                message=f"Permission denied reading skill '{name}'.",
                params_input=params_input,
                path_resolved=rel_path,
            )
        except OSError as exc:
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"Failed to read skill '{name}': {exc}",
                params_input=params_input,
                path_resolved=rel_path,
            )

        parsed = _parse_frontmatter(raw_content)
        if not parsed:
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"Skill '{name}' has invalid frontmatter.",
                params_input=params_input,
                path_resolved=rel_path,
            )

        _frontmatter, body = parsed
        expanded = _apply_arguments(body, args)
        base_dir = skill_meta.base_dir

        content = f"Base directory for this skill: {base_dir}\n\n{expanded}".strip()
        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        return self.create_success_response(
            data={
                "name": skill_meta.name,
                "base_dir": base_dir,
                "content": content,
            },
            text=f"Loaded skill '{skill_meta.name}'.",
            params_input=params_input,
            time_ms=elapsed_ms,
            path_resolved=rel_path,
        )


def _apply_arguments(body: str, args: str) -> str:
    trimmed_args = args.strip()
    if "$ARGUMENTS" in body:
        return body.replace("$ARGUMENTS", trimmed_args)
    if trimmed_args:
        return f"{body}\n\nARGUMENTS: {trimmed_args}"
    return body


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


__all__ = ["SkillTool"]
