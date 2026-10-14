"""Pin MCP v2 and its supported Streamable HTTP entrypoint."""
from importlib.metadata import version


def test_sdk_version_and_streamable_transport_compatibility():
    assert int(version("mcp").split(".")[0]) == 2
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client
    assert callable(streamable_http_client)
    assert callable(stdio_client)
    assert ClientSession is not None
