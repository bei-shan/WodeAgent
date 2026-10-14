# MCP v2 real-transport acceptance (2026-10-14)

The MCP suite has TWO live transports:
- stdio: genuine child JSON-RPC process, PID reuse and cleanup, v1+v2.
- HTTP: local ThreadingHTTPServer responding to initialize, tools/list,
  tools/call and GET 405, verifies a single initialize across sequential
  sync calls, async operations and clean shutdown.

Neither live test mocks the SDK ClientSession or accesses remote servers.
The HTTP fixture omits SSE resumption and OAuth. Timeouts and cancellation
cannot guarantee rollback of server side effects already started.
