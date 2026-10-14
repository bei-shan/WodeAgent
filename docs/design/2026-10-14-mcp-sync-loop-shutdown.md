# MCP sync-loop shutdown under concurrent requests

The per-client persistent event loop introduced with PR #49 retained a
shutdown leak: close_sync() skips loop.stop() if any other synchronous
request is in flight, and no caller later stopped the loop.

The client now marks its sync loop for shutdown when closing with active
requests. New sync submissions fail during that drain; the last completing
caller stops and clears the loop. Subsequent intentional use may start a
fresh loop. Real stdio/HTTP tests remain unchanged.

Regression tests coordinate a delayed async request with a concurrent
close_sync(), verify existing work can settle without a forced timeout,
verify new work is blocked while the loop drains, and check thread exit.
