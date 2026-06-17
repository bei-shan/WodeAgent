"""MCP configuration loader."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.env import load_env

load_env()

DEFAULT_CONFIG_FILES = (
    "mcp_servers.json",
    ".mcp.json",
    "mcp.json",
)


def _load_from_env() -> dict[str, Any] | None:
    raw = os.environ.get("MCP_SERVERS")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _load_from_files(project_root: str) -> dict[str, Any] | None:
    root = Path(project_root)
    for name in DEFAULT_CONFIG_FILES:
        path = root / name
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def load_mcp_servers(project_root: str) -> dict[str, Any]:
    data = _load_from_env() or _load_from_files(project_root)
    if not data:
        return {}
    if "mcpServers" in data and isinstance(data["mcpServers"], dict):
        return data["mcpServers"]
    if isinstance(data, dict):
        return data
    return {}


def connect_mode() -> str:
    # 默认改为 manual，避免启动时等待 MCP 子进程响应而阻塞
    # 启动阶段：后台线程连接，超时标记 pending，首次工具调用时重试
    return os.environ.get("MCP_CONNECT_MODE", "manual").lower()
