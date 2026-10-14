"""Offline MCP OAuth setup, callback validation and private token persistence."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from pathlib import Path

import pytest

from tools.mcp.client import MCPClientConfig
from tools.mcp.loader import _build_client_config
from tools.mcp.oauth import (
    FileOAuthTokenStorage, LoopbackOAuthFlow, make_oauth_http_client,
    validate_oauth_config,
)


@pytest.mark.parametrize("url", [
    "http://example.com/mcp", "file:///etc/passwd", "https://user:pass@host/mcp",
    "https://example.com/path#private", "http://192.168.1.1/mcp",
])
def test_non_https_remote_mcp_oauth_refused(url):
    with pytest.raises(ValueError):
        validate_oauth_config(url, {})


def test_auth_must_be_explicit_and_stdio_cannot_use_oauth(tmp_path):
    http_config = _build_client_config(str(tmp_path), {
        "transport": "http", "url": "https://mcp.example.com/mcp",
        "oauth": {"scope": "tasks:read", "callback_port": 8765},
    })
    assert http_config.oauth["scope"] == "tasks:read"
    assert http_config.project_root == str(tmp_path)
    assert _build_client_config(str(tmp_path), {
        "url": "https://mcp.example.com/mcp",
    }).oauth is None
    with pytest.raises(ValueError, match="HTTP"):
        _build_client_config(str(tmp_path), {
            "command": "python", "oauth": {"scope": "bad"},
        })


def test_token_store_preserves_fields_and_private_permissions(tmp_path):
    from mcp.shared.auth import OAuthToken, OAuthClientInformationFull

    async def run():
        store = FileOAuthTokenStorage(tmp_path, "https://example.com/mcp")
        assert await store.get_tokens() is None
        token = OAuthToken(access_token="token-value-not-for-logs", token_type="Bearer")
        await store.set_tokens(token)
        restored = await store.get_tokens()
        assert restored.access_token == token.access_token
        await store.set_tokens(OAuthToken(access_token="new-token", token_type="Bearer"))
        assert (await store.get_tokens()).access_token == "new-token"
        info = OAuthClientInformationFull(
            client_id="test-oauth-client",
            client_secret="test-only-secret",
        )
        await store.set_client_info(info)
        assert (await store.get_client_info()).client_id == "test-oauth-client"
        assert (await store.get_tokens()).access_token == "new-token"
        assert "new-token" in store.path.read_text()  # explicitly not encrypted
        assert store.path.parent.is_dir()
        if os.name != "nt":
            assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
            assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
        assert not list(store.directory.glob(".oauth-*.tmp"))
    asyncio.run(run())


def test_token_store_refuses_symlink_escape(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-oauth-secret")
    outside.mkdir()
    (tmp_path / ".mycodeagent").mkdir()
    (tmp_path / ".mycodeagent/oauth").symlink_to(outside, target_is_directory=True)
    store = FileOAuthTokenStorage(tmp_path, "https://mcp.example.com/mcp")
    with pytest.raises(ValueError, match="symbolic"):
        store._write("tokens", {"access_token": "do-not-leak"})
    assert not list(outside.iterdir())


def test_sdk_oauth_client_constructs_without_network_call(tmp_path):
    config = MCPClientConfig(
        transport="http", url="https://mcp.example.com/mcp",
        project_root=str(tmp_path), oauth={"scope": "notes:read"},
    )
    client, flow = make_oauth_http_client(config)
    assert flow is None
    assert client.auth is not None
    asyncio.run(client.aclose())
    assert not list(tmp_path.rglob("*.json"))


def test_loopback_callback_requires_matching_state_and_no_url_echo(capsys):
    async def run():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        flow = LoopbackOAuthFlow(port, open_browser=False)
        await flow.redirect("https://login.example.test/authorize?state=expected")
        assert "login.example.test" in capsys.readouterr().out

        async def get(path):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            await writer.drain()
            data = await asyncio.wait_for(reader.read(1000), timeout=2)
            writer.close()
            await writer.wait_closed()
            return data

        await get("/callback?code=bad&state=wrong")
        assert not flow._answer.done()
        good = await get("/callback?code=short-lived-code&state=expected")
        assert b"200 OK" in good
        assert b"short-lived-code" not in good
        code = await flow.callback()
        assert code.code == "short-lived-code"
        assert code.state == "expected"
        assert flow._server is None
    asyncio.run(run())


def test_callback_cleanup_on_wait_cancel(tmp_path):
    async def run():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        flow = LoopbackOAuthFlow(port, open_browser=False)
        await flow.redirect("https://login.example.test/authorize?state=s")
        task = asyncio.create_task(flow.callback())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert flow._server is None
    asyncio.run(run())
