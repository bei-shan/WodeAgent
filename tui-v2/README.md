# WodeAgent TUI v2 (experimental)

A TypeScript/Solid/OpenTUI terminal client for the **existing Python FastAPI Agent Engine**.

## Development
Start the Python service from the repository root:

    python -m uvicorn desktop.service.app:create_app --factory --host 127.0.0.1 --port 8000

In another terminal:

    cd tui-v2
    bun install
    bun run typecheck
    bun test
    bun run dev

Set WODEAGENT_SERVER=http://127.0.0.1:8000 to override the server address.

The client does not launch, alter, or embed Python. Keep the service on loopback for development;
the current FastAPI app is not authenticated for public exposure.

## Features in this phase
- Sessions and message history, new session, send, interruption, permissions and AskUser.
- Ordered WebSocket events with replay cursor, duplicate filtering, gap detection and reconnect.
- Pure event-to-view projection; no overrides of CodeAgent methods.
- Legacy Python TUI is preserved; this is an **experimental** alternative.

## Architecture
- src/protocol.ts: minimal wire DTOs and defensive event decoding
- src/api/: HTTP/WebSocket adapters; retry, cursor and subscription ownership
- src/state/: pure projection of events to the visible conversation
- src/controller.ts: per-session lifetime and command orchestration
- src/index.tsx: terminal rendering only

See docs/design/2026-10-13-tui-v2-layered-architecture.md.
