# Local Web Authentication Phase 1 — Single Operator

Implemented 2026-10-13; inspired by
[DeerFlow's global auth middleware](https://github.com/bytedance/deer-flow/blob/main/backend/app/gateway/auth_middleware.py),
[CSRF middleware](https://github.com/bytedance/deer-flow/blob/main/backend/app/gateway/csrf_middleware.py),
and its strict separation of authentication vs per-resource authorization.

## Purpose / boundaries

**This is an opt-in single-operator gate, not multi-user authentication or
authorization.** It makes a trusted local Web instance harder to access by
other processes or malicious browser origins. It does not isolate different
users, encrypt workspace files, sandbox the Python service itself or grant
authorization on a per-workspace basis.

The default configuration preserves the local development MVP without login.
To enable local operator login, set an unpredictable `WEB_AUTH_TOKEN` with
at least 24 characters before launching the FastAPI server.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# Set WEB_AUTH_TOKEN to that value in your PRIVATE .env.
uvicorn desktop.service.app:create_app --factory --host 127.0.0.1 --port 8000
cd desktop/web && npm ci && npm run dev
```

Open `http://localhost:5173` and enter the token. The frontend never writes
it to localStorage and never places it into a WebSocket URL. The login
endpoint returns an **HttpOnly, SameSite=Strict**, eight-hour HMAC-signed
cookie, marked `Secure` when served over HTTPS. A new server token invalidates
old cookies; logout removes the cookie.

## Enforced checks

- When auth enabled, REST requests, file download, session/control routes,
  tool config endpoints and `/docs` require a valid browser session cookie
  or an explicit API `Authorization: Bearer <WEB_AUTH_TOKEN>` header.
  Only `/api/auth/status` and `/api/auth/login` are public.
- State-changing cookie requests require an exact trusted local
  `Origin` (`localhost/127.0.0.1:5173` or `:8000`); other or missing
  origins fail closed. CLI API callers may omit Origin only if they supply an
  explicit Bearer token and no browser cookie. Host must be a strict
  loopback hostname and development port; this blocks common DNS rebinding
  attacks against browser-served local APIs.
- WebSocket session streams check both Origin and auth **before**
  accepting their handshake; no token in query parameters.
- The login endpoint limits attempts per address (5/minute) and compares
  secrets in constant time. Cookie signatures are HMAC-SHA256 with an
  eight-hour timestamp and a random nonce. Responses are `no-store`.

## Known limitations and next hardening work

- **Single principal only**. Every authenticated browser effectively is the
  same local operator, with access to all sessions. To support multiple
  users we must implement server-assigned user IDs, per-user session records,
  owner checks for REST and WebSocket, tool/permission policies, separate
  workspace mounts and scoped sandbox leases.
- Cookie security assumes a trusted local device and operating system.
  Any malware with local process privileges can access project files or
  the operator's `.env`. Treat the login token as a secret.
- Login uses a direct pre-shared token, not OIDC, MFA or per-user credentials.
  Rate limiting is process-local; use persistent throttling and security
  events before exposing a service remotely.
- The fixed Host/Origin allowlist intentionally limits ports 5173/8000.
  A custom reverse proxy, non-local deployment or custom port needs a
  reviewed explicit allowlist/TLS configuration; do not simply add `*`.
- **No public network exposure** until separate multi-user security reviews,
  persistent audit events and reverse-proxy/TLS deployment testing.
- This does not turn the existing `local_trusted` tool mode into a
  sandbox. Docker tool mode is independently opt-in.

## Verification

```bash
python -m pytest tests/test_local_web_auth.py -q
cd desktop/web && npx playwright test
```

Smoke tests validate wrong/correct token login, signed cookie validity,
logout, CSRF/Origin blocking, Bearer CLI path, WebSocket rejection, login
rate limit and Web UI pairing behavior. Tests use mock agents. The
security posture is explicitly **local, single-operator**.
