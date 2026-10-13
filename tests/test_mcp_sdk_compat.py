"""MCP v1 transport symbols are required by our current client adapter."""
from importlib.metadata import version


def test_sdk_version_and_streamable_transport_compatibility():
    parts = version("mcp").split(".")
    major, minor = int(parts[0]), int(parts[1])
    assert major == 1 and minor >= 10
    from mcp.client.streamable_http import streamablehttp_client
    assert callable(streamablehttp_client)
