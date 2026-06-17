"""MCP Loader tests — background connect, pending retry, and connect modes.

Run:
    python -m pytest tests/test_mcp_loader.py -v
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from tools.mcp.loader import (
    register_mcp_servers,
    retry_pending_server,
    get_pending_server_names,
    clear_pending_servers,
    _connect_one_server,
    _background_connect,
    _PendingServer,
)
from tools.mcp.client import MCPClient, MCPClientConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal fake for an MCP tool object."""

    def __init__(self, name, description="", schema=None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {}


class _FakeToolsResult:
    """Minimal fake for the result of list_tools()."""

    def __init__(self, tools):
        self.tools = tools


def _make_fake_registry():
    """Return a MagicMock that quacks like ToolRegistry for adapter use."""
    reg = MagicMock()
    reg.get_tool.return_value = None
    reg.get_function.return_value = None
    return reg


def _patch_env(**kwargs):
    """Context manager that patches os.environ temporarily."""
    return patch.dict(os.environ, kwargs, clear=False)


# ---------------------------------------------------------------------------
# Connect mode tests
# ---------------------------------------------------------------------------


class TestConnectMode(unittest.TestCase):
    """Test that connect modes are respected."""

    def setUp(self):
        clear_pending_servers()

    def tearDown(self):
        clear_pending_servers()

    @patch("tools.mcp.loader._background_connect")
    @patch("tools.mcp.loader.load_mcp_servers")
    @patch("tools.mcp.loader.connect_mode")
    def test_manual_mode_starts_background_threads(
        self, mock_mode, mock_load, mock_bg
    ):
        """In manual mode, _background_connect should be called (in a thread)."""
        mock_mode.return_value = "manual"
        mock_load.return_value = {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
        }
        mock_bg.side_effect = lambda *a, **kw: None  # no-op

        reg = _make_fake_registry()
        clients, tools_meta = register_mcp_servers(reg, "/fake/project")

        # tools_meta is empty initially in manual mode
        self.assertEqual(tools_meta, [])
        self.assertEqual(len(clients), 1)

        # _background_connect should have been called in a thread.
        # We need to wait briefly for the daemon thread to start and run.
        time.sleep(0.2)
        self.assertEqual(mock_bg.call_count, 1)

    @patch("tools.mcp.loader.register_mcp_tools")
    @patch("tools.mcp.loader.load_mcp_servers")
    @patch("tools.mcp.loader.connect_mode")
    def test_startup_mode_connects_synchronously(
        self, mock_mode, mock_load, mock_register
    ):
        """In startup mode, register_mcp_tools should be called directly."""
        mock_mode.return_value = "startup"
        mock_load.return_value = {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
        }
        mock_register.return_value = [
            {"name": "fetch_fetch", "description": "Fetch a URL"}
        ]

        reg = _make_fake_registry()
        clients, tools_meta = register_mcp_servers(reg, "/fake/project")

        self.assertEqual(len(tools_meta), 1)
        self.assertEqual(tools_meta[0]["name"], "fetch_fetch")
        self.assertEqual(len(clients), 1)

    @patch("tools.mcp.loader.load_mcp_servers")
    @patch("tools.mcp.loader.connect_mode")
    def test_disabled_mode_skips_all(self, mock_mode, mock_load):
        """In disabled mode, nothing should be registered."""
        mock_mode.return_value = "disabled"
        mock_load.return_value = {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
        }

        reg = _make_fake_registry()
        clients, tools_meta = register_mcp_servers(reg, "/fake/project")

        self.assertEqual(tools_meta, [])
        self.assertEqual(clients, [])

    @patch("tools.mcp.loader.load_mcp_servers")
    def test_no_servers_returns_empty(self, mock_load):
        """No servers configured should return empty lists."""
        mock_load.return_value = {}

        reg = _make_fake_registry()
        clients, tools_meta = register_mcp_servers(reg, "/fake/project")

        self.assertEqual(tools_meta, [])
        self.assertEqual(clients, [])


# ---------------------------------------------------------------------------
# Pending server management
# ---------------------------------------------------------------------------


class TestPendingServer(unittest.TestCase):
    """Test the _PendingServer dataclass and module-level pending registry."""

    def setUp(self):
        clear_pending_servers()

    def tearDown(self):
        clear_pending_servers()

    def test_pending_server_initial_state(self):
        """A fresh PendingServer should not be connected."""
        client = MCPClient(MCPClientConfig(transport="stdio", command="echo"))
        ps = _PendingServer("test-srv", client, "test-srv")
        self.assertFalse(ps.connected)
        self.assertIsNone(ps.error)
        self.assertEqual(ps.name, "test-srv")

    def test_get_pending_server_names_empty(self):
        """No pending servers by default."""
        self.assertEqual(get_pending_server_names(), [])

    def test_background_connect_adds_pending_on_timeout(self):
        """When _background_connect times out, server is added to pending list."""
        client = MagicMock(spec=MCPClient)
        client.connect_sync.side_effect = TimeoutError("connect timeout")

        reg = _make_fake_registry()
        _background_connect(reg, "slow-server", client, timeout=0.1)

        # Allow the daemon thread to finish
        time.sleep(0.3)

        pending = get_pending_server_names()
        self.assertIn("slow-server", pending)

    def test_background_connect_adds_pending_on_exception(self):
        """When _background_connect fails, server is added to pending list."""
        client = MagicMock(spec=MCPClient)
        client.connect_sync.side_effect = RuntimeError("boom")

        reg = _make_fake_registry()
        _background_connect(reg, "bad-server", client, timeout=1.0)

        time.sleep(0.3)

        pending = get_pending_server_names()
        self.assertIn("bad-server", pending)

    def test_clear_pending_servers(self):
        """clear_pending_servers removes all entries."""
        client = MagicMock(spec=MCPClient)
        client.connect_sync.side_effect = TimeoutError("timeout")

        reg = _make_fake_registry()
        _background_connect(reg, "srv1", client, timeout=0.1)
        _background_connect(reg, "srv2", client, timeout=0.1)
        time.sleep(0.3)

        self.assertEqual(len(get_pending_server_names()), 2)
        clear_pending_servers()
        self.assertEqual(get_pending_server_names(), [])


# ---------------------------------------------------------------------------
# Retry pending server
# ---------------------------------------------------------------------------


class TestRetryPendingServer(unittest.TestCase):
    """Test retry_pending_server function."""

    def setUp(self):
        clear_pending_servers()

    def tearDown(self):
        clear_pending_servers()

    def test_retry_nonexistent_returns_false(self):
        """Retrying a server that isn't pending returns False."""
        reg = _make_fake_registry()
        result = retry_pending_server(reg, "no-such-server", timeout=1.0)
        self.assertFalse(result)

    def test_retry_success_clears_pending(self):
        """Successful retry removes server from pending and registers tools."""
        client = MagicMock(spec=MCPClient)
        client.connect_sync.return_value = None
        client.list_tools_sync.return_value = _FakeToolsResult(
            [_FakeTool("do_thing", "Does a thing")]
        )
        client.config = MCPClientConfig(transport="stdio", command="echo")

        reg = _make_fake_registry()
        reg.get_tool.return_value = None
        reg.get_function.return_value = None

        # First, simulate a pending server
        with patch.dict(
            "tools.mcp.loader._pending_servers",
            {"test-srv": _PendingServer("test-srv", client, "test-srv")},
        ):
            result = retry_pending_server(reg, "test-srv", timeout=2.0)

        self.assertTrue(result)
        # After success, server should no longer be pending
        self.assertNotIn("test-srv", get_pending_server_names())

    def test_retry_failure_stays_pending(self):
        """Failed retry keeps server in pending list with error set."""
        client = MagicMock(spec=MCPClient)
        client.connect_sync.side_effect = RuntimeError("still broken")

        reg = _make_fake_registry()

        # Insert directly into module-level dict (avoid patch.dict which
        # restores the original on exit, wiping our mutation).
        from tools.mcp import loader as loader_mod

        ps = _PendingServer("bad-srv", client, "bad-srv")
        with loader_mod._pending_lock:
            loader_mod._pending_servers["bad-srv"] = ps

        try:
            result = retry_pending_server(reg, "bad-srv", timeout=1.0)

            self.assertFalse(result)
            # Server should still be pending
            self.assertIn("bad-srv", get_pending_server_names())
        finally:
            clear_pending_servers()


# ---------------------------------------------------------------------------
# _connect_one_server unit tests
# ---------------------------------------------------------------------------


class TestConnectOneServer(unittest.TestCase):
    """Test the _connect_one_server helper."""

    def setUp(self):
        clear_pending_servers()

    def tearDown(self):
        clear_pending_servers()

    def test_connect_success(self):
        """Successful connection returns tools_meta."""
        client = MagicMock(spec=MCPClient)
        client.connect_sync.return_value = None
        client.list_tools_sync.return_value = _FakeToolsResult(
            [_FakeTool("fetch", "Fetch URL", {"properties": {"url": {"type": "string"}}})]
        )
        client.config = MCPClientConfig(transport="stdio", command="uvx")

        reg = _make_fake_registry()
        reg.get_tool.return_value = None
        reg.get_function.return_value = None

        tools_meta = _connect_one_server(reg, "fetch", client, timeout=2.0)
        self.assertEqual(len(tools_meta), 1)
        self.assertEqual(tools_meta[0]["name"], "fetch_fetch")

    def test_connect_timeout_raises(self):
        """Connection that hangs raises TimeoutError."""
        client = MagicMock(spec=MCPClient)
        # simulate a very slow connect
        client.connect_sync.side_effect = lambda: time.sleep(10)

        reg = _make_fake_registry()

        with self.assertRaises(TimeoutError):
            _connect_one_server(reg, "slow", client, timeout=0.1)

    def test_connect_exception_propagates(self):
        """Connection error propagates to caller."""
        client = MagicMock(spec=MCPClient)
        client.connect_sync.side_effect = ConnectionError("refused")

        reg = _make_fake_registry()

        with self.assertRaises(ConnectionError):
            _connect_one_server(reg, "down", client, timeout=2.0)


# ---------------------------------------------------------------------------
# MCP_CONNECT_TIMEOUT env var
# ---------------------------------------------------------------------------


class TestConnectTimeoutEnv(unittest.TestCase):
    """Test that MCP_CONNECT_TIMEOUT env var is respected."""

    def setUp(self):
        clear_pending_servers()

    def tearDown(self):
        clear_pending_servers()

    def test_default_timeout(self):
        """Default timeout is 5 seconds when env var is not set."""
        # Re-import to pick up env
        import importlib
        import tools.mcp.loader

        importlib.reload(tools.mcp.loader)
        self.assertEqual(tools.mcp.loader._DEFAULT_CONNECT_TIMEOUT, 5.0)

    @patch.dict(os.environ, {"MCP_CONNECT_TIMEOUT": "3"}, clear=False)
    def test_custom_timeout_from_env(self):
        """MCP_CONNECT_TIMEOUT env var overrides default."""
        import importlib
        import tools.mcp.loader

        importlib.reload(tools.mcp.loader)
        self.assertEqual(tools.mcp.loader._DEFAULT_CONNECT_TIMEOUT, 3.0)


if __name__ == "__main__":
    unittest.main()
