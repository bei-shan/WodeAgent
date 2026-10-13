# MCP cooperative request cancellation

Earlier MCP input schema and validation hardening shipped in PR #36. Before
this change MCPClient used `asyncio.wait_for` (default 30 seconds for
`call_tool`) but did not receive `CodeAgent.interrupt()` cancellation;
users could wait for the full timeout after pressing Stop.

The existing per-Agent `scoped_tool_cancellation` ContextVar is now consulted
by MCPClient.call_tool_sync. With an active turn event, the client:
1. validates the already-checked cancellation state before dispatch,
2. starts the ordinary asynchronous `call_tool` coroutine,
3. polls that event, cancelling and awaiting the async task when signaled,
4. raises `InterruptedError` so the MCP adapter returns a stable
   `INTERRUPTED` tool envelope, never a generic network error.
5. keeps the original 30-second MCP timeout when no interruption occurs.

This design does not require any new MCP server capabilities, tool arguments,
or globally shared cancellation flags. Concurrent Agent events are isolated.

**Important:** Cancelling a local MCP client coroutine is best-effort. A
remote MCP server may already have performed non-reversible side effects.
An external server that ignores cancellation can continue running even
though Ridge Code stopped waiting. Authorization and sandbox enforcement
must still treat MCP tools as untrusted write-capable operations.

Regression tests use a live asyncio coroutine and demonstrate actual
`CancelledError` delivery, per-Agent isolation, cancellation before dispatch,
protocol response classification, and timeout/cancel separation.
