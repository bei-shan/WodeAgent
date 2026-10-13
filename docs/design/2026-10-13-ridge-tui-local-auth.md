# Ridge TUI local authentication

The terminal process can set RIDGECODE_AUTH_TOKEN (legacy:
WODEAGENT_AUTH_TOKEN) to the server's WEB_AUTH_TOKEN.

Transport:
- HTTP: Bearer header added to every request, including GET and POST.
- WebSocket: Bun-native headers sent during handshake, no URL/query tokens.
- No token appears in event stream, saved sessions, tracing or terminal UI.
- HTTP credentials sent to a remote host require HTTPS; loopback HTTP is
  permitted for local development.
- HTTP 401 and policy-closed WebSockets do not trigger token echo in errors.
- This is single-operator token auth; not multi-user identities or RBAC.

The Python service's existing local auth middleware accepts Bearer without
Origin for non-browser clients; browsers continue using HttpOnly cookies.
Do not embed the token in frontend bundles or share the session env.
