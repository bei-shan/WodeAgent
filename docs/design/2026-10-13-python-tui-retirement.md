# Python interactive TUI retirement (2026-10-13)

The user authorized removing the old Rich/prompt_toolkit terminal. The
sole maintained interactive terminal is Bun/Solid/OpenTUI in tui-v2/.
It uses the same Python FastAPI session, turn, events and permissions
as the React Web client.

Removed: tui/*, scripts/chat_test_agent.py, scripts/slash_commands.py,
utils/ui_components.py and tests exercising only these legacy displays.

Preserved: CodeAgent and Python runtime, session persistence, tool
registry, backend app, initial setup wizard, core Team CLI helpers,
configuration/data directory compatibility and React Web.

Migrated server-backed terminal commands: /new, /sessions, /use N,
/rename TITLE, /pin, /delete confirm, /models, /model PROFILE,
/plan on|off, /teams on|off, /help and /stop. Pending permission and AskUser
responses remain first-class; unknown slash commands are not sent to
the model.

Not equivalent to full old command parity: local-only /fork, /tree,
/style, /budget, advanced @ completion and manual JSON import/export
are out of scope until they have versioned service APIs and PTY tests.
The old launcher will no longer run. Existing snapshots are unaffected.

CI safeguards: full Web/API regressions, Bun typecheck/tests and an AST
import-boundary check to prevent reintroducing Python UI dependencies.
