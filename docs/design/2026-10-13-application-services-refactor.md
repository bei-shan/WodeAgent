# Phase 2 — Application Services extraction

Date: 2026-10-13

## Decision

Keep Python AgentEngine and HTTP/WebSocket contracts; separate session and turn use cases from FastAPI route functions.

The dependency direction is: HTTP routes (authentication and schema validation) -> framework-neutral Application Services -> SessionController (live workers/event journal) and SessionManager (persistent catalog).

- application/sessions.py: session creation, catalog, activation, metadata, history and cleanup.
- application/turns.py: turn status, execution commands, interruption, permission and AskUser.
- application/errors.py: typed use-case errors mapped by the HTTP adapter.
- SessionController.attach_session(): atomic attach of a known persisted ID without creating a dummy session, changing an ID, or assigning controller._sessions from the HTTP layer.

## Guarantees

1. Concurrent reconnects attach a single live session.
2. Existing HTTP paths, response bodies, turn.finished semantics and local auth behavior are preserved.
3. Session IDs are validated before workspace deletion.
4. User-provided titles are persisted at creation.
5. Application layer cannot import FastAPI, Starlette or frontend code; boundary tests enforce this.
6. Existing Web, auth, turn and sandbox tests remain part of CI.

## Deferred

Config / teams / file routers still contain business logic and will be migrated separately.
Process-wide mutable config should later be replaced with session-scoped explicit injection.
AgentLoop, ToolRuntime, ContextEngine ports and durable event storage are future independent phases.
