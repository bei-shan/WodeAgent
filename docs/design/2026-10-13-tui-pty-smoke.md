# Real PTY smoke acceptance

The Bun TUI test workflow exercises a real POSIX pseudoterminal, not only
headless Solid JSX rendering. The smoke test starts a loopback HTTP fixture
with session bootstrap endpoints and launches the actual OpenTUI entrypoint.

Checks:
- a 108x32 terminal displays the Ridge header
- the client invokes /api/health against the configured local server
- typing /help renders the server-backed session-tree command guidance
- Ctrl+C in an idle UI destroys the renderer and exits without an exception

This is an automated PTY bootstrap smoke test. It does not yet verify
interactive resize storms, alternate screen restoration on SIGTERM,
Windows terminal support, or a real LLM/tool exchange.
