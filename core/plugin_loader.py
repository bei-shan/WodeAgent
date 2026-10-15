"""Manifest-based project plugins with explicit enablement and code trust.

OpenCode-style deterministic discovery + Claude Code-style skill components.
No network/package installs and no Python/command execution during discovery.
Existing .mycode/plugins directories start disabled until explicitly enabled.
"""
from __future__ import annotations

import hashlib
import hmac
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
        if "custom_features" in parts:
            # There is no privileged Python Feature ABI in runtime v2.
            # Refusing manifests at install/discovery time prevents a plugin
            # from looking enabled while a dangerous capability is ignored.
            raise ValueError("Python plugin feature entrypoints are not supported")
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
                backup = self._backup_path(m.name)
                rows.append({
                    "name": m.name, "version": m.version,
                    "description": m.description,
                    "enabled": flags.get("enabled") is True,
                    "trusted": flags.get("trusted") is True,
                    "trust_valid": self.trust_valid(m, flags),
                    "skills": len(self.skill_dirs_for(m)),
                    "has_code": bool(m.hooks_config),
                    "source": flags.get("source", "local_directory"),
                    "sha256": flags.get("sha256"),
                    "pinned": bool(flags.get("pinned")),
                    "rollback_available": backup.is_dir() and _inside(self.root, backup),
                    "previous_version": flags.get("previous_version"),
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

    def _content_digest(self, manifest: PluginManifest) -> str:
        """Fingerprint reviewed plugin source without executing any code.

        Including all non-cache plugin assets prevents an attacker from
        replacing a shell hook or manifest while retaining prior approval.
        The runtime does not import project Python plugins.
        """
        directory = manifest.plugin_dir
        if not _inside(self.root, directory):
            raise ValueError("Unsafe plugin directory")
        digest = hashlib.sha256()
        total = 0
        count = 0
        for path in sorted(directory.rglob("*"), key=lambda x: x.as_posix()):
            if not _inside(self.root, path):
                raise ValueError("Plugin content includes an unsafe link")
            relative = path.relative_to(directory)
            if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
                continue
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("Plugin content includes a special file")
            count += 1
            if count > _MAX_FILES:
                raise ValueError("Plugin content exceeds file count limit")
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\\0")
            with path.open("rb") as handle:
                while chunk := handle.read(65536):
                    total += len(chunk)
                    if total > _MAX_EXTRACTED:
                        raise ValueError("Plugin content exceeds size limit")
                    digest.update(chunk)
            digest.update(b"\\0")
        return digest.hexdigest()

    def set_trusted(self, name: str, trusted: bool) -> dict:
        """Bind executable-code approval to the current on-disk source tree."""
        with _STATE_LOCK:
            manifest = self._find(name)
            state = self._state()
            flags = state.get(name, {})
            flags = flags if isinstance(flags, dict) else {}
            fingerprint = self._content_digest(manifest) if trusted else None
            state[name] = {
                **flags, "trusted": bool(trusted), "trusted_digest": fingerprint,
            }
            self._write_state(state)
        return {"name": name, "trusted": bool(trusted)}

    def trust_valid(self, manifest: PluginManifest, flags: dict) -> bool:
        expected = flags.get("trusted_digest")
        if not (flags.get("trusted") is True
                and isinstance(expected, str) and len(expected) == 64):
            return False
        try:
            return hmac.compare_digest(expected, self._content_digest(manifest))
        except (OSError, ValueError, RuntimeError):
            return False

    def enabled_manifests(self) -> list[PluginManifest]:
        state = self._state()
        return [
            m for m in self.manifests()
            if isinstance(state.get(m.name), dict)
            and state[m.name].get("enabled") is True
        ]

    def hooks_trusted(self, name: str) -> bool:
        # Operator opt-in is for explicitly reviewed command Hooks only.
        # The former RIDGE_ALLOW_PLUGIN_CODE switch grants nothing.
        if os.environ.get("RIDGE_ALLOW_TRUSTED_HOOKS", "").lower() not in ("1", "true", "yes"):
            return False
        try:
            manifest = self._find(name)
            flags = self._state().get(name)
            return isinstance(flags, dict) and self.trust_valid(manifest, flags)
        except (KeyError, ValueError):
            return False

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
                state[name] = {
                    "enabled": False, "trusted": False, "source": "local_zip",
                    "sha256": hashlib.sha256(archive).hexdigest(),
                    "pinned": False,
                }
                self._write_state(state)
        return {"status": "installed", "name": name, "enabled": False}

    def _backup_path(self, name: str) -> Path:
        return self.root / ".mycodeagent/plugins/backups" / name / "previous"

    @staticmethod
    def _check_digest(archive: bytes, expected_sha256: str | None) -> str:
        digest = hashlib.sha256(archive).hexdigest()
        if expected_sha256 is not None:
            if (not isinstance(expected_sha256, str)
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)
                    or not hmac.compare_digest(digest, expected_sha256.lower())):
                raise ValueError("Plugin ZIP SHA-256 checksum mismatch")
        return digest

    def set_pinned(self, name: str, pinned: bool) -> dict:
        with _STATE_LOCK:
            self._find(name)
            state = self._state()
            flags = state.get(name, {})
            if not isinstance(flags, dict) or not flags.get("sha256"):
                raise ValueError("Only verified ZIP installations can be pinned")
            flags["pinned"] = bool(pinned)
            state[name] = flags
            self._write_state(state)
        return {"name": name, "pinned": bool(pinned)}

    def update_zip(
        self, name: str, archive: bytes, *, expected_sha256: str | None = None,
    ) -> dict:
        """Staged, checksum-checked local update with one rollback slot.

        Code trust is revoked and plugin is disabled on every replacement.
        A pin blocks updates regardless of the manifest's version field.
        """
        import shutil
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("Invalid plugin name")
        if not isinstance(archive, bytes) or len(archive) > _MAX_ARCHIVE:
            raise ValueError("Plugin archive must be at most 2 MiB")
        digest = self._check_digest(archive, expected_sha256)
        with _STATE_LOCK:
            old = self._find(name)
            state = self._state()
            old_flags = state.get(name, {})
            old_flags = old_flags if isinstance(old_flags, dict) else {}
            if old_flags.get("pinned"):
                raise PermissionError("Unpin plugin before updating")
            destination = old.plugin_dir
            backup = self._backup_path(name)
            if (not _inside(self.root, destination) or
                    not _inside(self.root, backup)):
                raise ValueError("Unsafe plugin update path")
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".plugin-upgrade-", dir=self.plugins_dir,
            ) as staging:
                stage = Path(staging)
                # Reuse the full ZIP path/size/manifest validation. No code
                # can be imported or executed during this staging step.
                temp_catalog = PluginCatalog(stage)
                result = temp_catalog.install_zip(archive)
                if result["name"] != name:
                    raise ValueError("Plugin update must preserve package name")
                staged = stage / PLUGINS_DIR / name
                new_manifest = PluginManifest(
                    json.loads((staged / MANIFEST_FILE).read_text(encoding="utf-8"))
                    if (staged / MANIFEST_FILE).is_file() else
                    json.loads((staged / ".claude-plugin/plugin.json").read_text(encoding="utf-8")),
                    staged,
                )
                if new_manifest.version == old.version:
                    raise ValueError("Plugin update requires a new manifest version")

                backup.parent.mkdir(parents=True, exist_ok=True)
                rotated = backup.parent / "older"
                if rotated.exists():
                    shutil.rmtree(rotated)
                if backup.exists():
                    os.replace(backup, rotated)
                try:
                    os.replace(destination, backup)
                    try:
                        os.replace(staged, destination)
                        state[name] = {
                            "enabled": False, "trusted": False, "source": "local_zip",
                            "sha256": digest, "pinned": False,
                            "previous_version": old.version,
                            "previous_sha256": old_flags.get("sha256"),
                        }
                        self._write_state(state)
                    except BaseException:
                        if destination.exists():
                            shutil.rmtree(destination)
                        os.replace(backup, destination)
                        raise
                except BaseException:
                    if rotated.exists():
                        os.replace(rotated, backup)
                    raise
                if rotated.exists():
                    shutil.rmtree(rotated)
        return {"status": "updated", "name": name, "version": new_manifest.version,
                "enabled": False, "trusted": False, "sha256": digest}

    def rollback(self, name: str) -> dict:
        """Revert to the single retained previous revision, always disabled."""
        import shutil
        with _STATE_LOCK:
            current = self._find(name)
            backup = self._backup_path(name)
            if not _inside(self.root, backup) or not backup.is_dir():
                raise KeyError("No previous plugin revision")
            if not _inside(self.root, current.plugin_dir):
                raise ValueError("Unsafe plugin path")
            source = backup / MANIFEST_FILE
            if not source.exists():
                source = backup / ".claude-plugin/plugin.json"
            data = json.loads(source.read_text(encoding="utf-8"))
            manifest = PluginManifest(data, current.plugin_dir)
            flags = self._state().get(name, {})
            flags = flags if isinstance(flags, dict) else {}
            staged = backup.parent / ".rollback-current"
            if staged.exists():
                shutil.rmtree(staged)
            os.replace(current.plugin_dir, staged)
            try:
                os.replace(backup, current.plugin_dir)
                new_state = self._state()
                new_state[name] = {
                    "enabled": False, "trusted": False, "source": "local_zip",
                    "sha256": flags.get("previous_sha256"),
                    "pinned": False, "previous_version": current.version,
                    "previous_sha256": flags.get("sha256"),
                }
                self._write_state(new_state)
                os.replace(staged, backup)
            except BaseException:
                if current.plugin_dir.exists():
                    os.replace(current.plugin_dir, backup)
                os.replace(staged, current.plugin_dir)
                raise
        return {"status": "rolled_back", "name": name,
                "version": manifest.version, "enabled": False}

    def uninstall(self, name: str) -> dict:
        import shutil
        with _STATE_LOCK:
            m = self._find(name)
            if not _inside(self.root, m.plugin_dir):
                raise ValueError("Unsafe plugin path")
            shutil.rmtree(m.plugin_dir)
            backup = self._backup_path(name).parent
            if _inside(self.root, backup) and backup.is_dir():
                shutil.rmtree(backup)
            state = self._state()
            state.pop(name, None)
            self._write_state(state)
        return {"status": "deleted", "name": name}


class PluginLoader:
    """Discover trusted, declarative hook contributions without importing Python.

    Executable Python extension declarations fail manifest validation.
    This loader exposes only consented declarative Hook contributions.
    Skills and output styles remain catalogued through their own loaders.
    """

    def __init__(self, project_root: str):
        self.catalog = PluginCatalog(project_root)

    def list_plugins(self) -> list[dict[str, Any]]:
        return self.catalog.list_plugins()

    def discover(self) -> list["PluginHookContribution"]:
        contributions: list[PluginHookContribution] = []
        for manifest in self.catalog.enabled_manifests():
            # Enabled alone is insufficient for host command hooks.
            if not self.catalog.hooks_trusted(manifest.name):
                continue
            if manifest.hooks_config:
                contributions.append(PluginHookContribution(manifest))
        return contributions


class PluginHookContribution:
    """Declarative, operator-approved contribution; never executes on load."""

    name = "plugin_hooks"

    def __init__(self, manifest: PluginManifest):
        self._manifest = manifest

    def apply(self, hook_manager: Any) -> None:
        hook_manager.register_plugin_hooks(self._manifest.hooks_config)
