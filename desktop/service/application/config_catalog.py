"""Project configuration catalog use cases; no HTTP or UI dependencies.

Keeps persistent MCP servers, Hooks, and skill/MCP feature switches out of
FastAPI route functions. Serialized, atomic writes avoid lost concurrent
updates and partially written project config files.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from .errors import ConflictError, NotFoundError, ServiceError


class ConfigCatalogService:
    def __init__(self, project_root: str | Path):
        self.root = Path(project_root)
        self._lock = RLock()
        self._feature_path = self.root / "skill_state.json"
        self._mcp_path = self.root / "mcp_servers.json"
        self._hooks_path = self.root / ".mycode" / "hooks.json"

    @staticmethod
    def _read(path: Path, fallback: dict) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else fallback.copy()
        except (OSError, ValueError):
            return fallback.copy()

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic same-directory replace: readers either observe the old JSON
        # or the new JSON; interrupted writes leave no half-parsed config.
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".config-", suffix=".tmp", delete=False,
            ) as stream:
                temp = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        except OSError as exc:
            raise ServiceError("Unable to save project configuration") from exc
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def enabled(self, kind: str, name: str) -> dict:
        key = "disabled_skills" if kind == "skill" else "disabled_mcp"
        state = self._read(self._feature_path, {})
        return {"name": name, "enabled": not state.get(key, {}).get(name, False)}

    def toggle_enabled(self, kind: str, name: str) -> dict:
        key = "disabled_skills" if kind == "skill" else "disabled_mcp"
        with self._lock:
            state = self._read(self._feature_path, {})
            disabled = state.setdefault(key, {})
            currently = bool(disabled.get(name, False))
            if currently:
                disabled.pop(name, None)
            else:
                disabled[name] = True
            self._write(self._feature_path, state)
            return {"name": name, "enabled": currently}

    def list_mcp(self) -> list[dict]:
        with self._lock:
            servers = self._read(self._mcp_path, {"mcpServers": {}}).get("mcpServers", {})
            return [
                {"name": name, "command": cfg.get("command", ""), "args": cfg.get("args", [])}
                for name, cfg in servers.items()
            ]

    def create_mcp(self, name: str, command: str, args: list[str]) -> dict:
        with self._lock:
            data = self._read(self._mcp_path, {"mcpServers": {}})
            servers = data.setdefault("mcpServers", {})
            if name in servers:
                raise ConflictError(f"MCP server '{name}' already exists")
            servers[name] = {"command": command, "args": args}
            self._write(self._mcp_path, data)
            return {"status": "created", "name": name}

    def update_mcp(self, name: str, command: str | None, args: list[str] | None) -> dict:
        with self._lock:
            data = self._read(self._mcp_path, {"mcpServers": {}})
            servers = data.get("mcpServers", {})
            if name not in servers:
                raise NotFoundError(f"MCP server '{name}' not found")
            if command is not None:
                servers[name]["command"] = command
            if args is not None:
                servers[name]["args"] = args
            self._write(self._mcp_path, data)
            return {"status": "updated", "name": name}

    def delete_mcp(self, name: str) -> dict:
        with self._lock:
            data = self._read(self._mcp_path, {"mcpServers": {}})
            servers = data.get("mcpServers", {})
            if name not in servers:
                raise NotFoundError(f"MCP server '{name}' not found")
            del servers[name]
            self._write(self._mcp_path, data)
            return {"status": "deleted", "name": name}

    def get_hooks(self) -> dict:
        return self._read(self._hooks_path, {"hooks": {}})

    def save_hooks(self, body: dict) -> dict:
        with self._lock:
            self._write(self._hooks_path, body)
        return {"status": "saved"}
