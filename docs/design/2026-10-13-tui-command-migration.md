# TUI v2 command migration — 2026-10-13

The legacy Python Rich/prompt_toolkit terminal previously implemented
interactive slash commands locally in a separate Agent process.
The TypeScript/OpenTUI client uses application services over HTTP/WS;
terminal commands cannot directly mutate Python engine objects.

Migrated via the server's canonical API:
- /new, /sessions, /use N
- /rename TITLE, /pin, /delete confirm
- /models, /model PROFILE
- /plan on|off, /teams on|off
- /stop, /help

Model profile and plan/team updates require an idle turn. Permission and
AskUser pending requests override slash interpretation (an answer such as
"/help" is sent as an answer instead). Unknown slash commands are never
sent to the LLM. Delete requires typed confirmation.

Legacy-only commands such as /fork, /tree, /style, /budget and path-local
snapshot import/export are intentionally not advertised. They need versioned
server APIs and end-to-end tests before being reintroduced. The Web/API
supports the core chat/Agent flow without retaining a second Python UI.
