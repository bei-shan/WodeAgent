# WodeAgent modular architecture and TypeScript TUI v2 (2026-10-13)

## Decision
- Python remains the authoritative Agent Engine; **no reimplementation of the ReAct loop in TypeScript**.
- A separate Bun + TypeScript + OpenTUI/Solid terminal client communicates through the existing local FastAPI HTTP/WebSocket protocol.
- Legacy Python TUI remains available until migration and interactive terminal testing is complete.
- UI never subclasses CodeAgent or reads its private attributes.

## Target layers (dependency direction)

    OpenTUI (TS)    Web (React)     Future desktop
             \        |              /
              Client / DTO / Event API
                        |
    Application: SessionService / TurnService / PermissionService / EventJournal
                        |
    Domain: AgentLoop / ContextEngine / ToolRuntime / Teams
                        |
    Ports + adapters: Providers / MCP / Sandbox / Storage

- The client consumes typed wire DTOs and maintains its own pure, testable projection.
- The server owns session lifecycle, tools, approvals, cancellation and durable history.
- Infrastructure must not import a frontend module.
- On reconnect, event cursor replay is preferred; expired cursors require snapshot resync.
- A completed turn is determined by **turn.finished**, not run.finished or turn.completed.
- No model reasoning text should be exposed by default; the UI only displays content deltas.

## Migration milestones
1. Build TS protocol, HTTP/WebSocket client, pure event reducer and minimal OpenTUI app. Add Bun CI.
2. Add interactive lifecycle tests (permissions, AskUser, cancellation, subagent, resize, reconnect) using a fake server.
3. Extract application services from desktop/service/app.py; avoid mutable process-wide Config in per-session logic.
4. Extract AgentLoop/ToolRuntime with explicit ports and dependency injection; preserve context engine, team and tool compatibility.
5. Formalize sandbox + multi-user authentication and durable event storage, after first-party local-client flows are stable.

## Non-goals for this initial PR
- A second Agent implementation in TS.
- Replacing or breaking the existing Python TUI/Web clients.
- Claiming that the current local service is secure for public network exposure.
- Copying OpenCode source; only architectural boundaries are referenced.

## Quality gates
- TS pure event-projection tests, WebSocket cursor unit tests, TypeScript typecheck.
- GitHub Actions runs the above for any changes to tui-v2.
- Python existing tests and Web CI must continue passing before merge.
- Interactive terminal smoke tests are still required before promoting TUI v2 as default.
