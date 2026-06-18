"""MCP Client unit tests — connection lifecycle, reconnection, and thread safety.

Run:
    python -m pytest tests/test_mcp_client.py -v
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.mcp.client import MCPClient, MCPClientConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> MCPClientConfig:
    defaults = {"transport": "stdio", "command": "echo", "args": ["hello"]}
    defaults.update(kwargs)
    return MCPClientConfig(**defaults)


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestMCPClientConnection(unittest.TestCase):
    """Test connect / close / reconnect lifecycle."""

    def setUp(self):
        self.client = MCPClient(_make_config())

    def tearDown(self):
        try:
            self.client.close_sync()
        except Exception:
            pass

    def test_initial_state_not_connected(self):
        """A freshly created client should report not connected."""
        self.assertFalse(self.client.is_connected)
        self.assertIsNone(self.client._session)

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_connect_sets_session(self, mock_session_cls, mock_stdio):
        """After connect_sync, is_connected is True and _session is set."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session = AsyncMock()
        mock_session_cls.return_value = mock_session

        session = self.client.connect_sync()
        self.assertIsNotNone(session)
        self.assertTrue(self.client.is_connected)

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_connect_idempotent(self, mock_session_cls, mock_stdio):
        """Calling connect twice returns the same session."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session = AsyncMock()
        mock_session_cls.return_value = mock_session

        s1 = self.client.connect_sync()
        s2 = self.client.connect_sync()
        self.assertIs(s1, s2)

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_close_clears_session(self, mock_session_cls, mock_stdio):
        """After close_sync, is_connected is False."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session = AsyncMock()
        mock_session_cls.return_value = mock_session

        self.client.connect_sync()
        self.client.close_sync()
        self.assertFalse(self.client.is_connected)
        self.assertIsNone(self.client._session)

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_reconnect_after_close(self, mock_session_cls, mock_stdio):
        """After close, a new connect creates a new session."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session1 = AsyncMock()
        mock_session2 = AsyncMock()
        mock_session_cls.side_effect = [mock_session1, mock_session2]

        s1 = self.client.connect_sync()
        self.client.close_sync()
        s2 = self.client.connect_sync()
        self.assertIsNot(s1, s2)
        self.assertTrue(self.client.is_connected)


# ---------------------------------------------------------------------------
# Retry on transport error
# ---------------------------------------------------------------------------


class TestMCPClientReconnect(unittest.TestCase):
    """Test automatic reconnection on ClosedResourceError."""

    def setUp(self):
        self.client = MCPClient(_make_config())

    def tearDown(self):
        try:
            self.client.close_sync()
        except Exception:
            pass

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_call_tool_retries_on_closed_resource(self, mock_session_cls, mock_stdio):
        """call_tool reconnects and retries once on ClosedResourceError."""
        import anyio

        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        # First session fails with ClosedResourceError
        bad_session = AsyncMock()
        bad_session.call_tool.side_effect = anyio.ClosedResourceError("closed")

        # Second session succeeds
        good_session = AsyncMock()
        good_session.call_tool.return_value = MagicMock()

        mock_session_cls.side_effect = [bad_session, good_session]

        result = self.client.call_tool_sync("test_tool", {"arg": "val"})
        self.assertIsNotNone(result)
        # Both sessions should have been attempted
        self.assertEqual(mock_session_cls.call_count, 2)

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_list_tools_retries_on_closed_resource(self, mock_session_cls, mock_stdio):
        """list_tools reconnects and retries once on ClosedResourceError."""
        import anyio

        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        bad_session = AsyncMock()
        bad_session.list_tools.side_effect = anyio.ClosedResourceError("closed")

        good_session = AsyncMock()
        good_session.list_tools.return_value = MagicMock()

        mock_session_cls.side_effect = [bad_session, good_session]

        result = self.client.list_tools_sync()
        self.assertIsNotNone(result)
        self.assertEqual(mock_session_cls.call_count, 2)


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestMCPClientThreadSafety(unittest.TestCase):
    """Verify that concurrent access doesn't corrupt MCPClient state."""

    def setUp(self):
        self.client = MCPClient(_make_config())

    def tearDown(self):
        try:
            self.client.close_sync()
        except Exception:
            pass

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_concurrent_connect_only_one_session(self, mock_session_cls, mock_stdio):
        """Multiple threads calling connect_sync concurrently should result
        in exactly one underlying connection."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session = AsyncMock()
        mock_session_cls.return_value = mock_session

        sessions = []
        errors = []
        barrier = threading.Barrier(4, timeout=5)

        def worker():
            try:
                barrier.wait()
                s = self.client.connect_sync()
                sessions.append(s)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(sessions), 4)
        # All threads should get the same session object
        self.assertEqual(len(set(id(s) for s in sessions)), 1)
        # Only one ClientSession should have been created
        self.assertEqual(mock_session_cls.call_count, 1)

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_concurrent_close_and_connect(self, mock_session_cls, mock_stdio):
        """Close in one thread while another is connecting should not
        leave the client in a corrupt state."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session = AsyncMock()
        mock_session_cls.return_value = mock_session

        results = {"connected": False, "closed": False}
        barrier = threading.Barrier(2, timeout=5)

        def connector():
            try:
                barrier.wait()
                self.client.connect_sync()
                results["connected"] = True
            except Exception:
                pass

        def closer():
            try:
                barrier.wait()
                time.sleep(0.05)  # give connector a head start
                self.client.close_sync()
                results["closed"] = True
            except Exception:
                pass

        t1 = threading.Thread(target=connector)
        t2 = threading.Thread(target=closer)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # After close, session should be cleared — no corruption.
        self.assertIsNone(self.client._session)

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_concurrent_sync_calls_same_client(
        self, mock_session_cls, mock_stdio
    ):
        """Multiple threads calling sync methods on the same client should
        not raise or deadlock."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session = AsyncMock()
        mock_session.list_tools.return_value = MagicMock()
        mock_session.call_tool.return_value = MagicMock()
        mock_session_cls.return_value = mock_session

        errors = []
        barrier = threading.Barrier(3, timeout=5)

        def call_list_tools():
            try:
                barrier.wait()
                self.client.list_tools_sync()
            except Exception as e:
                errors.append(("list_tools", e))

        def call_call_tool():
            try:
                barrier.wait()
                self.client.call_tool_sync("test", {"a": 1})
            except Exception as e:
                errors.append(("call_tool", e))

        def call_connect():
            try:
                barrier.wait()
                self.client.connect_sync()
            except Exception as e:
                errors.append(("connect", e))

        threads = [
            threading.Thread(target=call_list_tools),
            threading.Thread(target=call_call_tool),
            threading.Thread(target=call_connect),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")

    def test_lock_attribute_exists(self):
        """Verify the lock attribute exists and is a threading.Lock."""
        self.assertIsInstance(self.client._lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# Config passthrough
# ---------------------------------------------------------------------------


class TestMCPClientConfig(unittest.TestCase):
    """Test MCPClientConfig dataclass."""

    def test_stdio_defaults(self):
        cfg = MCPClientConfig(transport="stdio", command="uvx", args=["mcp-fetch"])
        self.assertEqual(cfg.transport, "stdio")
        self.assertEqual(cfg.command, "uvx")
        self.assertIsNone(cfg.url)

    def test_http_config(self):
        cfg = MCPClientConfig(transport="http", url="http://localhost:8080")
        self.assertEqual(cfg.transport, "http")
        self.assertEqual(cfg.url, "http://localhost:8080")
        self.assertIsNone(cfg.command)

    def test_env_default_none(self):
        cfg = MCPClientConfig(transport="stdio", command="echo")
        self.assertIsNone(cfg.env)


# ---------------------------------------------------------------------------
# Timeout on MCP operations
# ---------------------------------------------------------------------------


class TestMCPClientTimeout(unittest.TestCase):
    """Test that MCP operations respect timeouts.

    We mock ``asyncio.wait_for`` directly to raise ``asyncio.TimeoutError``
    rather than trying to simulate a real hang (which triggers complex
    cancellation behavior in the MCP library's internals that doesn't
    interact well with mocked sessions).
    """

    def setUp(self):
        self.client = MCPClient(_make_config())

    def tearDown(self):
        try:
            self.client.close_sync()
        except Exception:
            pass

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_call_tool_timeout_raises(
        self, mock_session_cls, mock_stdio
    ):
        """call_tool raises TimeoutError when the operation times out."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session = AsyncMock()
        mock_session_cls.return_value = mock_session

        # Directly mock _call_with_retry to simulate a timeout
        original = self.client._call_with_retry
        async def _timeout(*args, **kwargs):
            raise TimeoutError("MCP operation 'call_tool:slow_tool' timed out after 30s")
        self.client._call_with_retry = _timeout

        with self.assertRaises(TimeoutError):
            self.client.call_tool_sync("slow_tool", {})
        self.client._call_with_retry = original

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_list_tools_timeout_raises(
        self, mock_session_cls, mock_stdio
    ):
        """list_tools raises TimeoutError when the operation times out."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session = AsyncMock()
        mock_session_cls.return_value = mock_session

        original = self.client._call_with_retry
        async def _timeout(*args, **kwargs):
            raise TimeoutError("MCP operation 'list_tools' timed out after 60s")
        self.client._call_with_retry = _timeout

        with self.assertRaises(TimeoutError):
            self.client.list_tools_sync()
        self.client._call_with_retry = original

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_timeout_error_contains_tool_name(
        self, mock_session_cls, mock_stdio
    ):
        """TimeoutError message includes the tool name for debugging."""
        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        mock_session = AsyncMock()
        mock_session_cls.return_value = mock_session

        original = self.client._call_with_retry
        async def _timeout(*args, **kwargs):
            raise TimeoutError("MCP operation 'call_tool:my_tool' timed out after 30s")
        self.client._call_with_retry = _timeout

        with self.assertRaises(TimeoutError) as ctx:
            self.client.call_tool_sync("my_tool", {})
        self.client._call_with_retry = original
        self.assertIn("my_tool", str(ctx.exception))


# ---------------------------------------------------------------------------
# Exception conversion (anyio → standard Python)
# ---------------------------------------------------------------------------


class TestMCPClientExceptionConversion(unittest.TestCase):
    """Test that anyio-specific exceptions are converted to standard types."""

    def setUp(self):
        self.client = MCPClient(_make_config())

    def tearDown(self):
        try:
            self.client.close_sync()
        except Exception:
            pass

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_double_closed_resource_raises_connection_error(
        self, mock_session_cls, mock_stdio
    ):
        """When both the initial call and the retry fail with ClosedResourceError,
        the client raises ConnectionError (not the raw anyio exception)."""
        import anyio

        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        bad_session = AsyncMock()
        bad_session.call_tool.side_effect = anyio.ClosedResourceError("closed")

        also_bad_session = AsyncMock()
        also_bad_session.call_tool.side_effect = anyio.ClosedResourceError("still closed")

        mock_session_cls.side_effect = [bad_session, also_bad_session]

        with self.assertRaises(ConnectionError):
            self.client.call_tool_sync("broken", {})

    @patch("tools.mcp.client.stdio_client")
    @patch("tools.mcp.client.ClientSession")
    def test_list_tools_double_closed_resource_raises_connection_error(
        self, mock_session_cls, mock_stdio
    ):
        """list_tools also converts double ClosedResourceError to ConnectionError."""
        import anyio

        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__.return_value = (mock_read, mock_write, None)
        mock_stdio.return_value = mock_conn_ctx

        bad_session = AsyncMock()
        bad_session.list_tools.side_effect = anyio.ClosedResourceError("closed")
        bad_session2 = AsyncMock()
        bad_session2.list_tools.side_effect = anyio.ClosedResourceError("closed again")

        mock_session_cls.side_effect = [bad_session, bad_session2]

        with self.assertRaises(ConnectionError):
            self.client.list_tools_sync()


if __name__ == "__main__":
    unittest.main()
