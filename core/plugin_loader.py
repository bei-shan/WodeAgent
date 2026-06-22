"""Plugin loader — discovers and loads plugins from ``.mycode/plugins/``.

Each plugin is a directory with a ``plugin.json`` manifest that declares
its features: hooks, skills, output_styles, and/or custom Python features.

Plugin features use the same :class:`AgentFeature` interface as built-in
features, so CodeAgent doesn't need to distinguish between them.

Usage::

    loader = PluginLoader(project_root=".")
    features = loader.discover()
    for feat in features:
        feat.init(agent)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.features.base import AgentFeature

if TYPE_CHECKING:
    from agents.codeAgent import CodeAgent

logger = logging.getLogger(__name__)

PLUGINS_DIR = ".mycode/plugins"
MANIFEST_FILE = "plugin.json"


class PluginManifest:
    """Parsed plugin.json manifest."""

    def __init__(self, data: dict[str, Any], plugin_dir: Path):
        self.name: str = str(data.get("name", plugin_dir.name))
        self.version: str = str(data.get("version", "0.1.0"))
        self.description: str = str(data.get("description", ""))
        self.plugin_dir: Path = plugin_dir

        # Feature flags in manifest
        features = data.get("features", {})
        if isinstance(features, dict):
            self.hooks_config: dict[str, Any] = features.get("hooks", {})
            self.skills_dirs: list[str] = self._as_str_list(features.get("skills", []))
            self.output_styles_dirs: list[str] = self._as_str_list(
                features.get("output_styles", [])
            )
            self.custom_features: list[str] = self._as_str_list(
                features.get("custom_features", [])
            )
        else:
            self.hooks_config = {}
            self.skills_dirs = []
            self.output_styles_dirs = []
            self.custom_features = []

        # Legacy top-level hooks (outside "features")
        hooks = data.get("hooks", {})
        if isinstance(hooks, dict) and hooks:
            self.hooks_config = {**self.hooks_config, **hooks}

    @staticmethod
    def _as_str_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str):
            return [value]
        return []


class PluginLoader:
    """Discovers and loads plugins from the project's plugin directory.

    Parameters
    ----------
    project_root:
        Project root directory.  Plugins are loaded from
        ``<project_root>/.mycode/plugins/``.
    """

    def __init__(self, project_root: str):
        self._root = Path(project_root)
        self._plugins_dir = self._root / PLUGINS_DIR

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self) -> list[AgentFeature]:
        """Discover and return all plugin features.

        Returns a list of :class:`AgentFeature` instances ready for
        ``init(agent)``.
        """
        if not self._plugins_dir.is_dir():
            return []

        features: list[AgentFeature] = []
        for plugin_dir in sorted(self._plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            if plugin_dir.name.startswith(".") or plugin_dir.name.startswith("_"):
                continue

            manifest = self._load_manifest(plugin_dir)
            if manifest is None:
                continue

            logger.info(
                "Loaded plugin: %s v%s (%s)",
                manifest.name, manifest.version, plugin_dir.name,
            )

            # Build features from manifest.
            features.extend(self._build_features(manifest))

        return features

    def list_plugins(self) -> list[dict[str, str]]:
        """Return metadata for all discovered plugins."""
        if not self._plugins_dir.is_dir():
            return []

        result: list[dict[str, str]] = []
        for plugin_dir in sorted(self._plugins_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            manifest = self._load_manifest(plugin_dir)
            if manifest is None:
                continue
            result.append({
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "directory": str(plugin_dir),
            })
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_manifest(self, plugin_dir: Path) -> PluginManifest | None:
        """Load and parse ``plugin.json`` from a plugin directory."""
        manifest_path = plugin_dir / MANIFEST_FILE
        if not manifest_path.is_file():
            logger.debug("No %s in %s", MANIFEST_FILE, plugin_dir)
            return None

        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to parse %s: %s", manifest_path, exc)
            return None

        if not isinstance(data, dict):
            logger.warning("%s is not a JSON object", manifest_path)
            return None

        return PluginManifest(data, plugin_dir)

    def _build_features(self, manifest: PluginManifest) -> list[AgentFeature]:
        """Build AgentFeature instances from a parsed manifest."""
        features: list[AgentFeature] = []

        # Hook feature
        if manifest.hooks_config:
            features.append(_PluginHookFeature(manifest))

        # Skill features
        for skills_dir in manifest.skills_dirs:
            features.append(_PluginSkillFeature(manifest, skills_dir))

        # Output style features
        for styles_dir in manifest.output_styles_dirs:
            features.append(_PluginOutputStyleFeature(manifest, styles_dir))

        # Custom Python features
        for feature_path in manifest.custom_features:
            feat = self._load_custom_feature(manifest, feature_path)
            if feat is not None:
                features.append(feat)

        return features

    def _load_custom_feature(
        self, manifest: PluginManifest, feature_path: str
    ) -> AgentFeature | None:
        """Load a custom Python feature module from the plugin directory.

        The module must export a class that inherits from AgentFeature.
        """
        full_path = manifest.plugin_dir / feature_path
        if not full_path.is_file() or not full_path.suffix == ".py":
            logger.warning(
                "Custom feature not found or not a .py file: %s", full_path
            )
            return None

        # Add plugin dir to path so the module can import sibling modules.
        plugin_dir_str = str(manifest.plugin_dir)
        if plugin_dir_str not in sys.path:
            sys.path.insert(0, plugin_dir_str)

        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                f"plugin_{manifest.name}_{full_path.stem}",
                str(full_path),
            )
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.warning("Failed to load custom feature %s: %s", full_path, exc)
            return None

        # Find the first AgentFeature subclass.
        for name in dir(module):
            obj = getattr(module, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, AgentFeature)
                and obj is not AgentFeature
            ):
                try:
                    return obj()
                except Exception as exc:
                    logger.warning(
                        "Failed to instantiate %s from %s: %s",
                        name, full_path, exc,
                    )
                    return None

        logger.warning("No AgentFeature subclass found in %s", full_path)
        return None


# ---------------------------------------------------------------------------
# Plugin feature adapters
# ---------------------------------------------------------------------------


class _PluginHookFeature(AgentFeature):
    """Wraps plugin-provided hooks as an AgentFeature.

    Merges plugin hooks into the global HookManager configuration.
    """

    name = "plugin_hooks"
    order = 86  # after built-in HookFeature (85)

    def __init__(self, manifest: PluginManifest):
        super().__init__()
        self._manifest = manifest

    def init(self, agent: "CodeAgent") -> None:
        """Write a merged hooks config to .mycode/hooks.json so HookManager picks it up."""
        if not self._manifest.hooks_config:
            return

        mycode_dir = self._manifest.plugin_dir.parent.parent  # .mycode/
        hooks_path = mycode_dir / "hooks.json"

        # Read existing hooks (if any).
        existing: dict[str, Any] = {"hooks": {}}
        if hooks_path.is_file():
            try:
                existing = json.loads(hooks_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        if not isinstance(existing, dict):
            existing = {"hooks": {}}
        if "hooks" not in existing or not isinstance(existing["hooks"], dict):
            existing["hooks"] = {}

        # Merge plugin hooks under a plugin-prefixed key.
        for event, matchers in self._manifest.hooks_config.items():
            if not isinstance(matchers, list):
                continue
            key = f"plugin:{self._manifest.name}:{event}"
            existing["hooks"][key] = matchers

        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))

        # Reload the hook manager so it picks up the merged config.
        if hasattr(agent, "_hook_manager"):
            agent._hook_manager.reload()


class _PluginSkillFeature(AgentFeature):
    """Registers plugin-provided skills with the SkillLoader."""

    name = "plugin_skills"
    order = 15  # before core skill loading

    def __init__(self, manifest: PluginManifest, skills_dir: str):
        super().__init__()
        self._manifest = manifest
        self._skills_dir = skills_dir

    def init(self, agent: "CodeAgent") -> None:
        """Copy or symlink plugin skills into the project skills directory."""
        src = self._manifest.plugin_dir / self._skills_dir
        if not src.is_dir():
            logger.warning("Plugin skill dir not found: %s", src)
            return

        # Refresh skills prompt so new skills appear.
        if hasattr(agent, "_refresh_skills_prompt"):
            try:
                agent._refresh_skills_prompt()
            except Exception:
                pass


class _PluginOutputStyleFeature(AgentFeature):
    """Registers plugin-provided output styles."""

    name = "plugin_output_styles"
    order = 81  # after built-in OutputStyleFeature (80)

    def __init__(self, manifest: PluginManifest, styles_dir: str):
        super().__init__()
        self._manifest = manifest
        self._styles_dir = styles_dir

    def init(self, agent: "CodeAgent") -> None:
        """Reload output styles so plugin styles are discovered."""
        src = self._manifest.plugin_dir / self._styles_dir
        if not src.is_dir():
            return

        if hasattr(agent, "_output_style_manager"):
            try:
                agent._output_style_manager.reload()
                # Re-sync to context builder.
                prompt = agent._output_style_manager.get_current_prompt()
                agent.context_builder.set_output_style_prompt(prompt)
            except Exception:
                pass
