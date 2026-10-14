# MCP Python SDK version compatibility (updated 2026-10-14)

## Current state

Default dependency: MCP Python SDK >=2,<3. The official v2 Streamable
HTTP entrypoint is streamable_http_client; the client consumes its
read/write stream tuple and normalizes snake_case result fields such as
is_error and structured_content. Stdio remains supported.

One CI lane installs MCP >=1.28,<2 and runs the adapter, mock contracts
and REAL localhost MCP transport tests, proving compatibility with
existing users while default installations use v2.

## Connection ownership

MCP transport and ClientSession contexts must enter and exit in the
same asyncio task. Agent sync tools schedule async requests on a single
per-client persistent event loop. Connection ownership stays in one
task until close, with bounded initialize, individual tool deadlines,
and cancellation propagation. This fixes the prior subprocess
respawning defect, demonstrated by real process PID regression tests.

## Live test coverage

- tests/test_mcp_stdio_live.py runs a genuine JSON-RPC subprocess and
  checks same PID over several calls, then closes it.
- tests/test_mcp_http_live.py runs local Streamable HTTP and checks
  single initialize, async/sync reuse, timeout, interrupt and teardown.
- tests/test_mcp_cancel_propagation.py tests independent Agent events.
- tests/test_mcp_sdk_compat.py asserts SDK v2 transport API.

## Remaining limits

Cancellation cannot roll back remote side effects. HTTP fixture does not
exercise OAuth, internet disconnects, SSE Last-Event-ID resumption, or
server-initiated requests. SDK v2 maps some failures to per-request MCPError;
do not equate all protocol errors with dead connections.

Historical note: previously requirements.txt pinned MCP <2 because the
removed v1 spelling streamablehttp_client was imported directly. That
restriction was replaced after an explicit migration.

Official guide:
https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md
