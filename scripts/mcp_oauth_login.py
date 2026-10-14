"""Explicit local, operator-initiated MCP OAuth authorization.

Run from a trusted interactive workstation:
  python scripts/mcp_oauth_login.py --server remote-mcp

This is NOT the Web user login flow. The browser redirects to a one-shot
127.0.0.1 callback; the official MCP SDK checks OAuth state and PKCE.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def login(name: str, project_root: Path) -> None:
    from tools.mcp.config import load_mcp_servers
    from tools.mcp.loader import _build_client_config
    from tools.mcp.client import MCPClient

    spec = load_mcp_servers(str(project_root)).get(name)
    if not isinstance(spec, dict):
        raise ValueError("Unknown MCP server; configure it in mcp_servers.json")
    config = _build_client_config(str(project_root), spec)
    if config.transport != "http" or config.oauth is None:
        raise ValueError("Selected server is not configured for HTTP OAuth")
    config = dataclasses.replace(config, oauth_interactive=True)
    client = MCPClient(config)
    try:
        await client.connect()
        print("MCP OAuth complete. Credentials saved for this project.")
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Configured HTTP OAuth MCP server name")
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print("Run OAuth login from an interactive local terminal.", file=sys.stderr)
        return 2
    try:
        asyncio.run(login(args.server, Path(args.project_root).resolve()))
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        # The OAuth provider may include URLs or secrets in error details:
        # never expose third-party raw exception messages.
        print(f"OAuth setup failed ({type(exc).__name__}). Check local MCP configuration.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
