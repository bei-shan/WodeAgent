# Web session stream ownership and session-switch races

Connection ownership moved from AppCore to state/useSessionStream.ts.
The hook tracks generation and active session id for socket callbacks,
disconnection, reconnect status rechecks and deferred history reload.

AppCore still owns session selection and view state; selecting another
session immediately disconnects the previous stream before HTTP awaits.
A per-selection version invalidates stale activate/history/config/teams
callbacks, including two selections of the same session id.

The event projection in state/sessionEvents.ts is unchanged. Neither
transport retry schedule nor backend events were modified.

Playwright tests replay delayed frames from a previously closed socket
after new-session selection and assert no cross-session UI corruption.
Existing tests cover reconnect cursors and dropped transport.
