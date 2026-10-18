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


def _build_client_config(
    project_root: str, spec: dict[str, Any], name: str = "",
) -> MCPClientConfig:
    transport = spec.get("transport")
    url = spec.get("url") or spec.get("endpoint")
    command = spec.get("command")
    args = spec.get("args") or []
    env = spec.get("env") or {}

    if transport == "http" or url:
        if not url:
            raise ValueError("MCP server config requires url for http transport")
        oauth = spec.get("oauth")
        if oauth is not None:
            from tools.mcp.oauth import validate_oauth_config
            validate_oauth_config(str(url), oauth)
        return MCPClientConfig(
            transport="http", name=name, url=url, env=env,
            oauth=oauth, project_root=str(Path(project_root).resolve()),
        )

    if spec.get("oauth") is not None:
        raise ValueError("OAuth applies only to HTTP MCP servers")
    env = _build_stdio_env(project_root, command, env)

    if not command:
        raise ValueError("MCP server config requires command for stdio transport")
    expanded_args = [os.path.expandvars(str(arg)) for arg in args]
    return MCPClientConfig(
        transport="stdio", name=name, command=command, args=expanded_args, env=env,
    )


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

    __slots__ = ("name", "client", "namespace", "connected", "error", "generation")

    def __init__(self, name: str, client: MCPClient, namespace: str, generation=None):
        self.name = name
        self.client = client
        self.namespace = namespace
        self.connected = False
        self.error: str | None = None
        self.generation = generation


# Pending connections are owned by their destination tool registry, not a
# process-wide server name. Two Agent sessions may use the same MCP name with
# different credentials/clients and must never recover each other's tools.
# Entries retain their registry while pending; cleanup must remove them.
_pending_servers: dict[tuple[object, str], _PendingServer] = {}
_pending_lock = threading.Lock()


class _StagedRegistry:
    """Discover adapters without publishing while remote I/O is in progress."""

    def __init__(self, registry):
        self.registry = registry
        self.batches = []

    def __getattr__(self, name):
        return getattr(self.registry, name)

    def replace_owned_tools(self, owner, adapters):
        self.batches.append((owner, adapters))

    def publish(self):
        replace_owned = getattr(self.registry, "replace_owned_tools", None)
        for owner, adapters in self.batches:
            if callable(replace_owned):
                replace_owned(owner, adapters)
            else:
                for adapter in adapters:
                    self.registry.register_tool(adapter)


def _generation_is_active(key, client, generation) -> bool:
    # None preserves the standalone connect/retry helpers' historical API.
    # Managed registrations always supply a token, even for the same client.
    return generation is None or (
        _shared_generations.get(key) is generation
        and _shared_clients.get(key) is client
    )


def _connect_one_server(
    tool_registry,
    server_name: str,
    client: MCPClient,
    timeout: float,
    generation=None,
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
    key = (tool_registry, server_name)

    def _check_active():
        with _shared_lock:
            if not _generation_is_active(key, client, generation):
                raise RuntimeError("MCP registration was released")

    def _work():
        lifetime = None
        try:
            # Admission and retirement share this lock. An admitted worker owns
            # the client until its finally block, including scheduler pauses
            # before the client's sync loop or transport has even been created.
            with _shared_lock:
                if not _generation_is_active(key, client, generation):
                    raise RuntimeError("MCP registration was released")
                if generation is not None:
                    lifetime = _client_lifetimes[client]
                    lifetime.workers += 1
            client.connect_sync()
            # Listing can lazily reopen a transport, so a connect that finished
            # after release must not start another remote operation.
            _check_active()
            staged = _StagedRegistry(tool_registry)
            tools_meta = register_mcp_tools(staged, client, namespace=server_name)
            # Connect/list above may outlive release. Only the active generation
            # can publish adapters, metadata, or consume its pending record.
            # Callers never join this worker while holding _shared_lock.
            with _shared_lock:
                if not _generation_is_active(key, client, generation):
                    raise RuntimeError("MCP registration was released")
                staged.publish()
                _shared_tools_meta[key] = list(tools_meta)
                with _pending_lock:
                    pending = _pending_servers.get(key)
                    if pending is not None and pending.client is client and pending.generation is generation:
                        _pending_servers.pop(key, None)
            result_container.append(tools_meta)
        except Exception as e:
            exc_container.append(e)
        finally:
            if lifetime is not None:
                with _shared_lock:
                    lifetime.workers -= 1
                    close = _claim_client_close(lifetime)
                if close:
                    _close_client_lifetime(client, lifetime)

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
    generation=None,
) -> None:
    """Run in a daemon thread: connect one server and register its tools.

    On success the server's tools become available to the agent.
    On timeout or failure the server is added to ``_pending_servers`` for
    later retry on first tool invocation.
    """
    try:
        tools_meta = _connect_one_server(tool_registry, server_name, client, timeout, generation)
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
    except Exception as exc:
        # Lock order is always shared -> pending. Do not reintroduce a retired
        # pending entry, including when the worker won a race with its timeout.
        key = (tool_registry, server_name)
        with _shared_lock:
            if not _generation_is_active(key, client, generation):
                return
            if generation is not None and key in _shared_tools_meta:
                return
            with _pending_lock:
                ps = _PendingServer(server_name, client, server_name, generation)
                ps.error = None if isinstance(exc, TimeoutError) else str(exc)
                _pending_servers[key] = ps
        logger.info(
            "MCP server '%s' connection failed: %s — "
            "marked as pending, will retry on first tool call",
            server_name,
            exc,
        )


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

    key = (tool_registry, server_name)
    with _pending_lock:
        ps = _pending_servers.get(key)

    if ps is None:
        # Not a pending server — maybe it was already connected or doesn't exist.
        return False

    try:
        tools_meta = _connect_one_server(
            tool_registry, server_name, ps.client, timeout, ps.generation
        )
        if tools_meta:
            logger.info(
                "MCP server '%s' connected on retry: %d tool(s) registered",
                server_name,
                len(tools_meta),
            )
        ps.connected = True
        ps.error = None
        # Remove only the exact object being retried; another concurrent
        # background reconnect may already have replaced this entry.
        with _pending_lock:
            if _pending_servers.get(key) is ps:
                _pending_servers.pop(key, None)
        return True
    except Exception as exc:
        logger.warning(
            "MCP server '%s' retry failed: %s",
            server_name,
            exc,
        )
        ps.error = str(exc)
        return False


def get_pending_server_names(tool_registry=None) -> list[str]:
    """Return pending server names for one registry (all registries if omitted)."""
    with _pending_lock:
        return sorted({name for (owner, name), pending in _pending_servers.items()
                       if (tool_registry is None or owner is tool_registry)
                       and not pending.connected})


def clear_pending_servers(tool_registry=None) -> None:
    """Discard pending connections owned by one Agent, or all in test cleanup."""
    with _pending_lock:
        if tool_registry is None:
            _pending_servers.clear()
        else:
            for key in tuple(_pending_servers):
                if key[0] is tool_registry:
                    _pending_servers.pop(key, None)


# ---------------------------------------------------------------------------
# Registry-scoped shared connections
# ---------------------------------------------------------------------------
#
# Custom hosts may give several Agents the same ToolRegistry. A server's
# connection and its published adapters
# therefore belong to the *registry*, not to whichever Agent connected first:
# closing one Agent may only drop that Agent's reference, and must never
# disconnect a server another Agent is still calling.

_shared_lock = threading.RLock()
_shared_clients: dict[tuple[Any, str], MCPClient] = {}
_shared_generations: dict[tuple[Any, str], object] = {}
_shared_tools_meta: dict[tuple[Any, str], list[dict[str, object | None]]] = {}
_shared_users: dict[Any, int] = {}


class _ClientLifetime:
    """Own a client across registration generations and admitted workers."""

    def __init__(self):
        self.registrations = 0
        self.workers = 0
        self.closing = False


_client_lifetimes: dict[MCPClient, _ClientLifetime] = {}


def _claim_client_close(lifetime: _ClientLifetime) -> bool:
    """Called with _shared_lock; exactly one caller claims the eventual close."""
    if lifetime.registrations or lifetime.workers or lifetime.closing:
        return False
    lifetime.closing = True
    return True


def _close_client_lifetime(client: MCPClient, lifetime: _ClientLifetime) -> None:
    """No global lock across transport/loop shutdown; reject reuse while closing."""
    try:
        client.close_sync()
    except Exception as exc:
        logger.debug("MCP client close failed: %s", exc)
    finally:
        with _shared_lock:
            if _client_lifetimes.get(client) is lifetime:
                _client_lifetimes.pop(client, None)


def release_mcp_registration(tool_registry) -> bool:
    """Detach one Agent from its registry's shared MCP connections.

    Only the last Agent using a registry retires its servers: their adapters are
    unregistered (so stale model snapshots reject the removed handles) and the
    clients are disconnected.  Registries that never registered MCP are left
    untouched, never swept.

    An admitted connection worker retains its client until connect/list exits
    under the client's own I/O deadlines. Even after the outer join times out,
    the last worker owns the eventual close; retiring public registry state
    never abandons that resource or closes it before a paused worker starts I/O.

    Returns ``True`` when a shared registration was held and released, and
    ``False`` when this registry owns nothing — the caller then still owns
    whatever clients it holds itself.
    """
    clients = []
    invalidation_error = None
    try:
        with _shared_lock:
            users = _shared_users.get(tool_registry)
            if users is None:
                return False
            users -= 1
            if users > 0:
                _shared_users[tool_registry] = users
                return True
            _shared_users.pop(tool_registry, None)
            names = [name for (registry, name) in _shared_clients if registry is tool_registry]
            for name in names:
                client = _shared_clients.pop((tool_registry, name))
                lifetime = _client_lifetimes[client]
                lifetime.registrations -= 1
                if _claim_client_close(lifetime):
                    clients.append((client, lifetime))
                _shared_generations.pop((tool_registry, name), None)
                _shared_tools_meta.pop((tool_registry, name), None)

            # Retire registry state before another registration can publish.
            # Manual discovery may have published nothing before a sandbox seal;
            # do not request structural changes for nonexistent owners.
            clear_pending_servers(tool_registry)
            unregister_owner = getattr(tool_registry, "unregister_owner", None)
            if callable(unregister_owner):
                list_owners = getattr(tool_registry, "registered_owners", None)
                owners = set(list_owners(prefix="mcp:")) if callable(list_owners) else None
                for name in names:
                    owner = f"mcp:{name}"
                    if owners is not None and owner not in owners:
                        continue
                    try:
                        unregister_owner(owner)
                    except Exception as exc:
                        # Keep the registry's structural policy intact. Attempt
                        # our remaining owners, then report the first failure.
                        if invalidation_error is None:
                            invalidation_error = exc
                        logger.warning("MCP owner invalidation failed for %s: %s", owner, exc)
        if invalidation_error is not None:
            raise invalidation_error
    finally:
        # Registry invalidation can fail (notably under a sandbox seal). Every
        # already-claimed close must still run, outside the shared lock.
        for client, lifetime in clients:
            _close_client_lifetime(client, lifetime)
    return True


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
    failed_clients: list[tuple[MCPClient, _ClientLifetime]] = []
    registered_tools: list[dict[str, object | None]] = []

    # Held across connection setup so concurrent Agent init cannot spawn a
    # second subprocess for a server the registry already owns.
    with _shared_lock:
        for server_name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            key = (tool_registry, server_name)
            existing = _shared_clients.get(key)
            if existing is not None:
                # Already connected for this registry: hand the same client to
                # this Agent and skip re-discovery, so its adapters stay intact.
                clients.append(existing)
                registered_tools.extend(_shared_tools_meta.get(key, []))
                continue

            try:
                config = _build_client_config(project_root, spec, name=server_name)
                client = MCPClient(config)
                lifetime = _client_lifetimes.get(client)
                if lifetime is not None and lifetime.closing:
                    raise RuntimeError("MCP client is closing; a fresh client is required")
            except Exception as exc:
                # A bad server must not strand earlier connections before the
                # caller receives its clients and acquires a shared reference.
                logger.warning("MCP client setup failed for %s: %s", server_name, exc)
                continue
            if lifetime is None:
                lifetime = _ClientLifetime()
                _client_lifetimes[client] = lifetime
            lifetime.registrations += 1
            _shared_clients[key] = client
            generation = object()
            _shared_generations[key] = generation

            if mode == "startup":
                # Original synchronous behavior — connect now, block until done.
                try:
                    tools_meta = register_mcp_tools(
                        tool_registry, client, namespace=server_name
                    )
                    _shared_tools_meta[key] = list(tools_meta)
                    registered_tools.extend(tools_meta)
                except Exception as exc:
                    logger.warning(
                        "MCP tool registration failed for %s: %s", server_name, exc
                    )
                    # Never cache a client that published nothing: the next
                    # Agent must be free to try connecting again.
                    _shared_clients.pop(key, None)
                    _shared_generations.pop(key, None)
                    lifetime.registrations -= 1
                    if _claim_client_close(lifetime):
                        failed_clients.append((client, lifetime))
                    continue
            elif mode == "manual":
                # Background connect — don't block agent init.
                t = threading.Thread(
                    target=_background_connect,
                    args=(tool_registry, server_name, client, _DEFAULT_CONNECT_TIMEOUT, generation),
                    daemon=True,
                    name=f"mcp-connect-{server_name}",
                )
                t.start()
            # else: unknown mode — skip registration (same as disabled)
            clients.append(client)

        # Only an Agent that actually received clients holds a reference; an
        # empty result must stay symmetric with the release in MCPService.
        if clients:
            _shared_users[tool_registry] = _shared_users.get(tool_registry, 0) + 1

    # A failed discovery may already have opened a transport. It has no shared
    # owner and must not escape to MCPService, whose release skips such clients.
    for client, lifetime in failed_clients:
        _close_client_lifetime(client, lifetime)

    return clients, registered_tools
