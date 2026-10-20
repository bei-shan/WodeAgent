"""MCP configuration loader."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from core.env import load_env, get_operator_env

load_env()

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILES = (
    "mcp_servers.json",
    ".mcp.json",
    "mcp.json",
)


def _load_from_env() -> dict[str, Any] | None:
    raw = get_operator_env("MCP_SERVERS")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("MCP_SERVERS env var contains invalid JSON: %s", exc)
        return None


def _load_from_files(project_root: str) -> dict[str, Any] | None:
    root = Path(project_root)
    for name in DEFAULT_CONFIG_FILES:
        path = root / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            logger.info("MCP config loaded from %s", path)
            return data
        except json.JSONDecodeError as exc:
            logger.warning(
                "MCP config file %s has invalid JSON at line %d column %d: %s",
                path, exc.lineno, exc.colno, exc.msg,
            )
            # JSON syntax error means the user intended to configure MCP
            # but made a mistake.  Don't silently fall through to the next
            # file — surface the error and stop.
            return None
        except PermissionError:
            logger.warning("MCP config file %s: permission denied, skipping", path)
            continue
        except OSError as exc:
            logger.warning("MCP config file %s: %s, skipping", path, exc)
            continue
        except Exception as exc:
            logger.warning(
                "MCP config file %s: unexpected error: %s, skipping", path, exc
            )
            continue
    return None


def load_mcp_servers(project_root: str) -> dict[str, Any]:
    # A repository can supply .mcp.json and request execution of arbitrary
    # stdio processes during "manual" background connection. Never treat
    # repository-controlled configuration as an operator trust grant.
    data = _load_from_env()
    if not data:
        if (get_operator_env("MCP_TRUST_PROJECT_CONFIG") or "").strip().lower() not in {
            "true", "1", "yes", "on",
        }:
            # Fail closed before parsing/connecting any project-defined server.
            # MCP_SERVERS is the operator-supplied out-of-repo alternative.
            if any((Path(project_root) / name).exists() for name in DEFAULT_CONFIG_FILES):
                logger.warning(
                    "Project MCP configuration ignored. Set MCP_TRUST_PROJECT_CONFIG=true "
                    "only after reviewing all defined servers and executable commands."
                )
            return {}
        data = _load_from_files(project_root)
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
