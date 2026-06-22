"""Output Styles — load, manage, and provide style-specific system prompts.

Supports three built-in styles (default, explanatory, learning) loaded from
``prompts/output_styles/*.md``, plus custom styles from the project root's
``output_styles/`` directory (Markdown files with YAML frontmatter).

Usage::

    manager = OutputStyleManager(project_root=".", env_style="explanatory")
    manager.get_current()         # "explanatory"
    manager.get_current_prompt()  # "# Output Style: explanatory\\n..."
    manager.set_current("learning")
    manager.list_all()            # {"default": "...", "explanatory": "...", ...}
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_STYLE = "default"
BUILTIN_STYLES_DIR = "prompts/output_styles"
CUSTOM_STYLES_DIR = "output_styles"

# Regex to parse YAML-like frontmatter (simpler than pyyaml dependency).
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


@dataclass
class OutputStyleDefinition:
    """A single output style definition."""

    name: str
    description: str
    prompt: str
    source: str  # "builtin" | "project"
    keep_coding_instructions: bool = True


class OutputStyleManager:
    """Manages output style loading, selection, and prompt generation.

    Parameters
    ----------
    project_root:
        The project root directory used to discover custom style files.
    env_style:
        Initial style name from ``AGENT_OUTPUT_STYLE`` env var.
        Defaults to ``"default"`` if ``None`` or empty.
    """

    def __init__(
        self,
        project_root: str,
        env_style: Optional[str] = None,
    ):
        self._project_root = Path(project_root)
        self._styles: dict[str, OutputStyleDefinition] = {}
        self._current: str = DEFAULT_STYLE

        # Load built-in + custom styles.
        self._load_all()

        # Apply env var override.
        if env_style and env_style.strip():
            resolved = self.resolve_name(env_style.strip())
            if resolved:
                self._current = resolved
            else:
                logger.warning(
                    "AGENT_OUTPUT_STYLE=%s not found, using default", env_style
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current(self) -> str:
        """Return the currently active style name."""
        return self._current

    def get_current_prompt(self) -> str:
        """Return the system-prompt text for the current style.

        Returns an empty string for the ``default`` style.
        """
        style = self._styles.get(self._current)
        if style is None or not style.prompt.strip():
            return ""
        return (
            f"\n\n# Output Style: {style.name}\n{style.prompt.strip()}"
        )

    def get_style(self, name: str) -> Optional[OutputStyleDefinition]:
        """Return the style definition for *name*, or ``None``."""
        return self._styles.get(name)

    def set_current(self, name: str) -> bool:
        """Set the active style by name.  Returns ``True`` on success."""
        resolved = self.resolve_name(name)
        if resolved is None:
            return False
        self._current = resolved
        return True

    def list_all(self) -> dict[str, str]:
        """Return ``{name: description}`` for all available styles."""
        return {n: s.description for n, s in self._styles.items()}

    def resolve_name(self, name: str) -> Optional[str]:
        """Case-insensitive style name lookup.

        Returns the canonical name or ``None``.
        """
        if not name or not name.strip():
            return None
        name = name.strip()
        if name in self._styles:
            return name
        lower = name.lower()
        for key in self._styles:
            if key.lower() == lower:
                return key
        return None

    def reload(self) -> None:
        """Re-scan custom style directories for new or changed files."""
        self._load_all()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """Load built-in styles first, then overlay custom (project) styles."""
        self._styles = {}
        self._styles.update(self._load_builtin_styles())
        self._styles.update(self._load_custom_styles())
        # Ensure DEFAULT_STYLE always exists.
        if DEFAULT_STYLE not in self._styles:
            self._styles[DEFAULT_STYLE] = OutputStyleDefinition(
                name=DEFAULT_STYLE,
                description="默认风格，保持简洁高效的输出",
                prompt="",
                source="builtin",
            )

    def _load_builtin_styles(self) -> dict[str, OutputStyleDefinition]:
        """Load styles from ``prompts/output_styles/*.md``."""
        builtin_dir = self._project_root / BUILTIN_STYLES_DIR
        return self._load_from_dir(builtin_dir, "builtin")

    def _load_custom_styles(self) -> dict[str, OutputStyleDefinition]:
        """Load styles from ``{project_root}/output_styles/*.md``."""
        custom_dir = self._project_root / CUSTOM_STYLES_DIR
        if not custom_dir.is_dir():
            return {}
        return self._load_from_dir(custom_dir, "project")

    def _load_from_dir(
        self, directory: Path, source: str
    ) -> dict[str, OutputStyleDefinition]:
        """Parse all ``.md`` files in *directory* and return a style map."""
        styles: dict[str, OutputStyleDefinition] = {}
        if not directory.is_dir():
            return styles

        for md_file in sorted(directory.glob("*.md")):
            definition = self._parse_style_file(md_file, source)
            if definition is not None:
                styles[definition.name] = definition
        return styles

    def _parse_style_file(
        self, path: Path, source: str
    ) -> Optional[OutputStyleDefinition]:
        """Parse a single Markdown style file.

        Expected format::

            ---
            name: my-style
            description: A short description
            keep_coding_instructions: true
            ---

            Style-specific system prompt content...
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read style file %s: %s", path, exc)
            return None

        m = _FRONTMATTER_RE.match(text)
        if m is None:
            # No frontmatter — use filename as name, first line as description.
            name = path.stem
            prompt = text.strip()
            description = self._extract_description(prompt, f"Custom {name} style")
            return OutputStyleDefinition(
                name=name,
                description=description,
                prompt=prompt,
                source=source,
            )

        frontmatter_text = m.group(1)
        prompt = m.group(2).strip()

        # Parse frontmatter key: value pairs.
        fm = self._parse_frontmatter(frontmatter_text)
        name = fm.get("name") or path.stem
        description = fm.get("description") or self._extract_description(
            prompt, f"Custom {name} style"
        )
        keep = fm.get("keep_coding_instructions", "true").lower() in (
            "true", "yes", "1",
        )

        return OutputStyleDefinition(
            name=name,
            description=description,
            prompt=prompt,
            source=source,
            keep_coding_instructions=keep,
        )

    @staticmethod
    def _parse_frontmatter(text: str) -> dict[str, str]:
        """Parse simple ``key: value`` YAML-like frontmatter.

        Intentionally simple — avoids pyyaml dependency for a handful of keys.
        """
        result: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    result[key] = value
        return result

    @staticmethod
    def _extract_description(prompt: str, fallback: str) -> str:
        """Extract a short description from the first non-empty line of *prompt*."""
        if not prompt:
            return fallback
        for line in prompt.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped[:100]
        return fallback
