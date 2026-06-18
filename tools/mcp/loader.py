"""Register MCP servers and tools in ToolRegistry.

Supports three connect modes (``MCP_CONNECT_MODE`` env):

* ``startup`` — connect synchronously at agent init (original behavior).
* ``manual``  — connect in background threads with a per-server timeout;
  tools are registered as soon as each background connection succeeds.
  Servers that time out are marked *pending* and will be retried on first
  tool invocation.
* ``disabled`` — skip MCP entirely.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from tools.mcp.client import MCPClient, MCPClientConfig
from tools.mcp.adapter import register_mcp_tools
from tools.mcp.config import load_mcp_servers, connect_mode

logger = logging.getLogger(__name__)

# Per-server connect timeout for background threads (seconds).
# Default 30s — npx-based servers need 10-30s for first-time package downloads.
# Configurable via MCP_CONNECT_TIMEOUT env var.
_DEFAULT_CONNECT_TIMEOUT = float(os.environ.get("MCP_CONNECT_TIMEOUT", "30"))


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _ensure_cache_dirs(project_root: str) -> tuple[Path, Path, Path]:
    """创建缓存目录并返回路径，调用方按需使用。"""
    root = Path(project_root)
    uv_cache = root / ".uv_cache"
    uv_tools = root / ".uv_tools"
    npm_cache = root / ".npm_cache"
    for d in (uv_cache, uv_tools, npm_cache):
        d.mkdir(parents=True, exist_ok=True)
    return uv_cache, uv_tools, npm_cache


def _build_stdio_env(project_root: str, command: str, env: dict[str, str] | None) -> dict[str, str]:
    """根据命令类型注入对应的缓存环境变量，让 uvx/npx 使用项目级缓存。"""
    merged = dict(env or {})
    uv_cache, uv_tools, npm_cache = _ensure_cache_dirs(project_root)

    if command in {"uvx", "uv"}:
        merged.setdefault("UV_CACHE_DIR", str(uv_cache))
        merged.setdefault("XDG_CACHE_HOME", str(uv_cache))
        merged.setdefault("UV_HOME", str(uv_tools))
        merged.setdefault("UV_TOOL_DIR", str(uv_tools))
        merged.setdefault("UV_TOOL_BIN_DIR", str(uv_tools / "bin"))
        if "UV_LOCK_TIMEOUT" not in merged and "UV_LOCK_TIMEOUT" not in os.environ:
            merged["UV_LOCK_TIMEOUT"] = "10"

    if command in {"npx", "npm", "node"}:
        merged.setdefault("NPM_CONFIG_CACHE", str(npm_cache))
        merged.setdefault("NPM_CONFIG_LOGLEVEL", "error")
        merged.setdefault("NPM_CONFIG_FUND", "false")
        merged.setdefault("NPM_CONFIG_AUDIT", "false")

    return merged


def _build_client_config(project_root: str, spec: dict[str, Any]) -> MCPClientConfig:
    transport = spec.get("transport")
    url = spec.get("url") or spec.get("endpoint")
    command = spec.get("command")
    args = spec.get("args") or []
    env = spec.get("env") or {}

    if transport == "http" or url:
        if not url:
            raise ValueError("MCP server config requires url for http transport")
        return MCPClientConfig(transport="http", url=url, env=env)

    env = _build_stdio_env(project_root, command, env)

    if not command:
        raise ValueError("MCP server config requires command for stdio transport")
    expanded_args = [os.path.expandvars(str(arg)) for arg in args]
    return MCPClientConfig(transport="stdio", command=command, args=expanded_args, env=env)


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


def _format_schema(schema: object | None) -> str:
    if not isinstance(schema, dict):
        return ""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not isinstance(properties, dict) or not properties:
        return ""
    parts = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            parts.append(str(name))
            continue
        type_name = spec.get("type")
        default = spec.get("default")
        desc = (spec.get("description") or "").strip()
        required_flag = " required" if name in required else ""
        type_label = f": {type_name}" if type_name else ""
        default_label = f", default={default}" if default is not None else ""
        if desc:
            parts.append(f"{name}{type_label}{default_label}{required_flag} - {desc}")
        else:
            parts.append(f"{name}{type_label}{default_label}{required_flag}")
    return "; ".join(parts)


def format_mcp_tools_prompt(tools_meta: list[dict[str, object | None]]) -> str:
    if not tools_meta:
        return ""
    lines = []
    for item in tools_meta:
        name = item.get("name") or ""
        description = (item.get("description") or "").strip()
        schema_text = _format_schema(item.get("schema"))
        if description:
            lines.append(f"- {name}: {description}")
        else:
            lines.append(f"- {name}")
        if schema_text:
            lines.append(f"  params: {schema_text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Background connection machinery (manual / lazy mode)
# ---------------------------------------------------------------------------


class _PendingServer:
    """Holds state for an MCP server that hasn't connected yet."""

    __slots__ = ("name", "client", "namespace", "connected", "error")

    def __init__(self, name: str, client: MCPClient, namespace: str):
        self.name = name
        self.client = client
        self.namespace = namespace
        self.connected = False
        self.error: str | None = None


# Module-level registry of pending servers, keyed by server name.
# Populated in manual mode, consumed by ``retry_pending_server`` when a tool
# from an unconnected server is invoked for the first time.
_pending_servers: dict[str, _PendingServer] = {}
_pending_lock = threading.Lock()


def _connect_one_server(
    tool_registry,
    server_name: str,
    client: MCPClient,
    timeout: float,
) -> list[dict[str, object | None]]:
    """Connect *client*, discover tools, register them.  Called from a
    background thread or synchronously on retry.

    Returns the tools_meta list on success; raises on failure.
    """
    # connect_sync() will block until the session is established.
    # We wrap it with a timeout via a separate thread so we can enforce
    # the per-server deadline.
    result_container: list[Any] = []
    exc_container: list[Exception] = []

    def _work():
        try:
            client.connect_sync()
            tools_meta = register_mcp_tools(tool_registry, client, namespace=server_name)
            result_container.append(tools_meta)
        except Exception as e:
            exc_container.append(e)

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Timeout — the thread is still running. We cannot kill it safely
        # in Python, but we mark the server as pending so it will be retried
        # on first tool invocation.
        raise TimeoutError(
            f"MCP server '{server_name}' did not connect within {timeout:.0f}s"
        )

    if exc_container:
        raise exc_container[0]

    return result_container[0] if result_container else []


def _background_connect(
    tool_registry,
    server_name: str,
    client: MCPClient,
    timeout: float,
) -> None:
    """Run in a daemon thread: connect one server and register its tools.

    On success the server's tools become available to the agent.
    On timeout or failure the server is added to ``_pending_servers`` for
    later retry on first tool invocation.
    """
    try:
        tools_meta = _connect_one_server(tool_registry, server_name, client, timeout)
        if tools_meta:
            logger.info(
                "MCP server '%s' connected in background: %d tool(s) registered",
                server_name,
                len(tools_meta),
            )
        else:
            logger.info(
                "MCP server '%s' connected in background: no tools discovered",
                server_name,
            )
    except TimeoutError:
        logger.info(
            "MCP server '%s' connection timed out after %.0fs — "
            "marked as pending, will retry on first tool call",
            server_name,
            timeout,
        )
        with _pending_lock:
            _pending_servers[server_name] = _PendingServer(
                name=server_name, client=client, namespace=server_name
            )
    except Exception as exc:
        logger.info(
            "MCP server '%s' connection failed: %s — "
            "marked as pending, will retry on first tool call",
            server_name,
            exc,
        )
        with _pending_lock:
            ps = _PendingServer(name=server_name, client=client, namespace=server_name)
            ps.error = str(exc)
            _pending_servers[server_name] = ps


def retry_pending_server(
    tool_registry,
    server_name: str,
    timeout: float | None = None,
) -> bool:
    """Retry connecting a pending server synchronously.

    Called when a tool from a not-yet-connected MCP server is about to be
    executed.  Returns ``True`` if the server is now connected and its tools
    are registered.

    Args:
        tool_registry: The ``ToolRegistry`` to register tools into.
        server_name: The MCP server name (namespace).
        timeout: Override for the per-server connect timeout.

    Returns:
        ``True`` if the server connected successfully, ``False`` otherwise.
    """
    if timeout is None:
        timeout = _DEFAULT_CONNECT_TIMEOUT

    with _pending_lock:
        ps = _pending_servers.get(server_name)

    if ps is None:
        # Not a pending server — maybe it was already connected or doesn't exist.
        return False

    try:
        tools_meta = _connect_one_server(
            tool_registry, server_name, ps.client, timeout
        )
        if tools_meta:
            logger.info(
                "MCP server '%s' connected on retry: %d tool(s) registered",
                server_name,
                len(tools_meta),
            )
        ps.connected = True
        ps.error = None
        return True
    except Exception as exc:
        logger.warning(
            "MCP server '%s' retry failed: %s",
            server_name,
            exc,
        )
        ps.error = str(exc)
        return False


def get_pending_server_names() -> list[str]:
    """Return names of servers that are still pending (not yet connected)."""
    with _pending_lock:
        return sorted(_pending_servers.keys())


def clear_pending_servers() -> None:
    """Remove all pending server entries (for testing)."""
    with _pending_lock:
        _pending_servers.clear()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def register_mcp_servers(
    tool_registry,
    project_root: str,
) -> tuple[list[MCPClient], list[dict[str, object | None]]]:
    """Discover and register MCP tools according to the current connect mode.

    Returns ``(clients, tools_meta)``.  In ``manual`` mode ``tools_meta`` may
    be empty at first — tools appear as background connections complete.
    """
    servers = load_mcp_servers(project_root)
    mode = connect_mode()
    if not servers or mode == "disabled":
        return [], []

    clients: list[MCPClient] = []
    registered_tools: list[dict[str, object | None]] = []

    for server_name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        config = _build_client_config(project_root, spec)
        client = MCPClient(config)
        clients.append(client)

        if mode == "startup":
            # Original synchronous behavior — connect now, block until done.
            try:
                tools_meta = register_mcp_tools(
                    tool_registry, client, namespace=server_name
                )
                registered_tools.extend(tools_meta)
            except Exception as exc:
                logger.warning(
                    "MCP tool registration failed for %s: %s", server_name, exc
                )
                continue
        elif mode == "manual":
            # Background connect — don't block agent init.
            t = threading.Thread(
                target=_background_connect,
                args=(tool_registry, server_name, client, _DEFAULT_CONNECT_TIMEOUT),
                daemon=True,
                name=f"mcp-connect-{server_name}",
            )
            t.start()
        # else: unknown mode — skip registration (same as disabled)

    return clients, registered_tools
