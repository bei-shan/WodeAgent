"""Manifest-based project plugins with explicit enablement and code trust.

OpenCode-style deterministic discovery + Claude Code-style skill components.
No network/package installs and no Python/command execution during discovery.
Existing .mycode/plugins directories start disabled until explicitly enabled.
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import re
import stat
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from core.features.base import AgentFeature

logger = logging.getLogger(__name__)
PLUGINS_DIR = ".mycode/plugins"
MANIFEST_FILE = "plugin.json"
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MAX_ARCHIVE = 2 * 1024 * 1024
_MAX_EXTRACTED = 6 * 1024 * 1024
_MAX_FILES = 128
_STATE_LOCK = threading.RLock()


def _inside(root: Path, path: Path) -> bool:
    """Lexical + canonical path check: reject symlinked ancestors."""
    try:
        rel = path.relative_to(root)
        current = root
        if root.is_symlink():
            return False
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                return False
        return path.resolve().is_relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return False


def _paths(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("Plugin component paths must be a list")
    paths = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("Invalid plugin component path")
        pure = PurePosixPath(item)
        if (pure.is_absolute() or ".." in pure.parts or "\\" in item
                or any(x in ("", ".") for x in item.split("/"))
                or len(pure.parts) > 5):
            raise ValueError("Plugin component path must be a safe relative path")
        paths.append(item)
    return paths


class PluginManifest:
    """Validated, non-executable plugin.json component declaration."""

    def __init__(self, data: dict[str, Any], plugin_dir: Path):
        name = data.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name != plugin_dir.name:
            raise ValueError("Plugin name must match its kebab-case directory")
        self.name = name
        self.version = str(data.get("version", "0.1.0"))[:50]
        self.description = str(data.get("description", ""))[:300]
        self.plugin_dir = plugin_dir
        parts = data.get("features", {})
        if not isinstance(parts, dict):
            raise ValueError("Plugin features must be an object")
        default_skills = ["skills"] if (plugin_dir / "skills").is_dir() else []
        self.skills_dirs = _paths(parts.get("skills", default_skills))
        self.output_styles_dirs = _paths(parts.get("output_styles", []))
        self.custom_features = _paths(parts.get("custom_features", []))
        hooks = parts.get("hooks", data.get("hooks", {}))
        if not isinstance(hooks, dict):
            raise ValueError("Plugin hooks must be an object")
        self.hooks_config = hooks


class PluginCatalog:
    """Filesystem catalog; explicit persistent enable/trust state per project."""

    def __init__(self, project_root: str | Path):
        self.root = Path(project_root).resolve()
        self.plugins_dir = self.root / PLUGINS_DIR
        self.state_path = self.root / ".mycodeagent/plugins/state.json"

    def _state(self) -> dict:
        if not _inside(self.root, self.state_path):
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, data: dict) -> None:
        if not _inside(self.root, self.state_path):
            raise ValueError("Unsafe plugin state directory")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.state_path.parent,
                prefix=".plugin-state-", suffix=".tmp", delete=False,
            ) as stream:
                temp = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.state_path)
        finally:
            if temp:
                temp.unlink(missing_ok=True)

    def manifests(self) -> list[PluginManifest]:
        if not self.plugins_dir.is_dir() or not _inside(self.root, self.plugins_dir):
            return []
        found = []
        for directory in sorted(self.plugins_dir.iterdir()):
            if (not _NAME.fullmatch(directory.name) or not directory.is_dir()
                    or not _inside(self.root, directory)):
                continue
            paths = (directory / MANIFEST_FILE, directory / ".claude-plugin/plugin.json")
            manifest_file = next((p for p in paths if p.is_file() and _inside(self.root, p)), None)
            if manifest_file is None:
                continue
            try:
                if manifest_file.stat().st_size > 64 * 1024:
                    continue
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                found.append(PluginManifest(data, directory))
            except (ValueError, OSError, TypeError) as exc:
                logger.warning("Skipping invalid plugin %s: %s", directory.name, type(exc).__name__)
        return found

    def _find(self, name: str) -> PluginManifest:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("Invalid plugin name")
        for m in self.manifests():
            if m.name == name:
                return m
        raise KeyError("Plugin not found")

    def list_plugins(self) -> list[dict[str, Any]]:
        with _STATE_LOCK:
            states = self._state()
            rows = []
            for m in self.manifests():
                flags = states.get(m.name, {})
                flags = flags if isinstance(flags, dict) else {}
                rows.append({
                    "name": m.name, "version": m.version,
                    "description": m.description,
                    "enabled": flags.get("enabled") is True,
                    "trusted": flags.get("trusted") is True,
                    "skills": len(self.skill_dirs_for(m)),
                    "has_code": bool(m.custom_features or m.hooks_config),
                })
            return rows

    def set_enabled(self, name: str, enabled: bool) -> dict:
        with _STATE_LOCK:
            self._find(name)
            state = self._state()
            flags = state.get(name, {})
            flags = flags if isinstance(flags, dict) else {}
            state[name] = {**flags, "enabled": bool(enabled)}
            self._write_state(state)
        return {"name": name, "enabled": bool(enabled)}

    def set_trusted(self, name: str, trusted: bool) -> dict:
        """Explicit user trust, separate from enabling a data-only plugin."""
        with _STATE_LOCK:
            self._find(name)
            state = self._state()
            flags = state.get(name, {})
            flags = flags if isinstance(flags, dict) else {}
            state[name] = {**flags, "trusted": bool(trusted)}
            self._write_state(state)
        return {"name": name, "trusted": bool(trusted)}

    def enabled_manifests(self) -> list[PluginManifest]:
        state = self._state()
        return [
            m for m in self.manifests()
            if isinstance(state.get(m.name), dict)
            and state[m.name].get("enabled") is True
        ]

    def code_trusted(self, name: str) -> bool:
        flags = self._state().get(name)
        return (isinstance(flags, dict) and flags.get("trusted") is True
                and os.environ.get("RIDGE_ALLOW_PLUGIN_CODE", "").lower() in ("1", "true", "yes"))

    def skill_dirs_for(self, m: PluginManifest) -> list[Path]:
        result = []
        for rel in m.skills_dirs:
            path = m.plugin_dir / rel
            if path.is_dir() and _inside(self.root, path):
                result.append(path)
        return result

    def active_skill_dirs(self) -> list[Path]:
        return [p for m in self.enabled_manifests() for p in self.skill_dirs_for(m)]

    def install_zip(self, archive: bytes) -> dict:
        """Install a local zip only. Never fetch, import or run during install."""
        if not isinstance(archive, bytes) or len(archive) > _MAX_ARCHIVE:
            raise ValueError("Plugin archive must be at most 2 MiB")
        if not _inside(self.root, self.plugins_dir):
            raise ValueError("Unsafe plugin installation directory")
        try:
            zipped = zipfile.ZipFile(io.BytesIO(archive))
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise ValueError("Invalid plugin ZIP") from exc
        with zipped:
            infos = zipped.infolist()
            if len(infos) > _MAX_FILES or not infos:
                raise ValueError("Too many plugin files")
            total = 0
            for info in infos:
                parts = PurePosixPath(info.filename)
                if (parts.is_absolute() or ".." in parts.parts or "\\" in info.filename
                        or not info.filename or "\0" in info.filename
                        or any(part.startswith(".") and part != ".claude-plugin" for part in parts.parts)):
                    raise ValueError("Unsafe plugin archive path")
                kind = (info.external_attr >> 16) & 0o170000
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("Plugin archive contains a link or special file")
                total += info.file_size
                if total > _MAX_EXTRACTED:
                    raise ValueError("Plugin extracted content exceeds limit")
            manifest_name = next(
                (n for n in ("plugin.json", ".claude-plugin/plugin.json")
                 if n in zipped.namelist()), None,
            )
            if manifest_name is None:
                raise ValueError("Plugin ZIP must contain a root manifest")
            try:
                data = json.loads(zipped.read(manifest_name))
            except (ValueError, UnicodeError) as exc:
                raise ValueError("Invalid plugin manifest") from exc
            if not isinstance(data, dict):
                raise ValueError("Invalid plugin manifest")
            name = data.get("name")
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError("Invalid plugin name")
            destination = self.plugins_dir / name
            with _STATE_LOCK:
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError("Plugin is already installed")
                self.plugins_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".plugin-staging-", dir=self.plugins_dir) as staging:
                    root = Path(staging)
                    for info in infos:
                        target = root.joinpath(*PurePosixPath(info.filename).parts)
                        if info.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with zipped.open(info) as source, target.open("xb") as output:
                                while chunk := source.read(65536):
                                    output.write(chunk)
                    PluginManifest(data, root / name)  # validate under final directory name
                    # Manifest validation must use the *installed* plugin basename.
                    os.replace(root, destination)
                    # TemporaryDirectory cleanup is harmless after rename.
                state = self._state()
                state[name] = {"enabled": False, "trusted": False}
                self._write_state(state)
        return {"status": "installed", "name": name, "enabled": False}

    def uninstall(self, name: str) -> dict:
        import shutil
        with _STATE_LOCK:
            m = self._find(name)
            if not _inside(self.root, m.plugin_dir):
                raise ValueError("Unsafe plugin path")
            shutil.rmtree(m.plugin_dir)
            state = self._state()
            state.pop(name, None)
            self._write_state(state)
        return {"status": "deleted", "name": name}


class PluginLoader:
    """Feature discovery, with inert inspection unless explicitly trusted."""

    def __init__(self, project_root: str):
        self.catalog = PluginCatalog(project_root)

    def list_plugins(self) -> list[dict[str, Any]]:
        return self.catalog.list_plugins()

    def discover(self) -> list[AgentFeature]:
        features: list[AgentFeature] = []
        for m in self.catalog.enabled_manifests():
            # Skills are discovered independently by SkillLoader on each scan.
            # Hooks and Python code require BOTH operator env and explicit trust.
            if not self.catalog.code_trusted(m.name):
                continue
            if m.hooks_config:
                features.append(_PluginHookFeature(m))
            for rel in m.custom_features:
                feature = self._load_custom_feature(m, rel)
                if feature:
                    features.append(feature)
        return features

    def _load_custom_feature(self, manifest: PluginManifest, rel: str) -> AgentFeature | None:
        source = manifest.plugin_dir / rel
        if (source.suffix != ".py" or not source.is_file()
                or not _inside(self.catalog.root, source)):
            return None
        try:
            spec = importlib.util.spec_from_file_location(
                "ridge_plugin_" + manifest.name.replace("-", "_") + "_" + source.stem,
                source,
            )
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for value in vars(module).values():
                if (isinstance(value, type) and issubclass(value, AgentFeature)
                        and value is not AgentFeature and value.__module__ == module.__name__):
                    return value()
        except Exception:
            logger.warning("Failed to load trusted plugin feature %s", manifest.name)
        return None


class _PluginHookFeature(AgentFeature):
    """In-memory hook contribution. Never mutate .mycode/hooks.json."""

    name = "plugin_hooks"
    order = 86

    def __init__(self, manifest: PluginManifest):
        super().__init__()
        self._manifest = manifest

    def init(self, agent: Any) -> None:
        hook_manager = getattr(agent, "_hook_manager", None)
        if hook_manager is not None:
            hook_manager.register_plugin_hooks(self._manifest.hooks_config)
