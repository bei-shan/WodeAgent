# MCP v2 real-transport acceptance (2026-10-14)

The MCP suite has TWO live transports:
- stdio: genuine child JSON-RPC process, PID reuse and cleanup, v1+v2.
- HTTP: local ThreadingHTTPServer responding to initialize, tools/list,
  tools/call and GET 405, verifies a single initialize across sequential
  sync calls, async operations and clean shutdown.

Neither live test mocks the SDK ClientSession or accesses remote servers.
The HTTP fixture omits SSE resumption and OAuth. Timeouts and cancellation
cannot guarantee rollback of server side effects already started.

## Ambiguous side-effect delivery (2026-10-14)

A broken transport after a tool request was sent leaves the remote outcome
UNKNOWN. Tool calls are never automatically replayed after transport loss;
inspect remote state before initiating a retry. The client discards the
broken session so subsequent user-initiated actions can reconnect. Read-only
MCP queries still retry once after connection loss. Timeout cancellation is
not evidence that the remote side effect was rolled back.

### SDK-level internal retry limitation

The real HTTP lost-reply fixture demonstrates that MCP SDK v1/v2 may
reissue transport requests internally after an HTTP disconnect. Preventing
OUR application-layer replay reduces one source of duplicate side effects,
but **does not provide exactly-once tool execution**. The caller receives
an uncertain outcome; any operation that requires strict at-most-once must
use server-supported idempotency keys or remote state reconciliation.
Avoid issuing non-idempotent commands blindly after timeout or EOF.
