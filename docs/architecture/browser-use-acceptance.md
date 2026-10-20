# Browser Use trust boundaries and test gates (2026-10-20)

This is **experimental local-trusted** Browser Use, not a multi-tenant/isolated
Agent browser and not a claim of production-grade OS/computer control.

## Operator-only enablement

1. Install `pip install -r requirements-browser.txt` and `python -m playwright install chromium`.
2. Set `AGENT_BROWSER_ENABLED=true`, `AGENT_BROWSER_ALLOWED_ORIGINS=https://your-approved-site.example`.
3. Start a **local, trusted** Ridge Code Agent. Config is operator-owned, not a model argument.
4. `BrowserUse` supports `navigate`, `observe`, `click`, `fill`. Page text
   is **untrusted** tool data. `click` and `fill` need a fresh user approval
   via the Agent's trusted UI broker on every call, with exact selector/value.
   Headless browser runtime is initialized lazily and closed with the owning Agent.

Default is disabled; no browser is auto-registered when disabled. Hosted Agent,
`AGENT_REQUIRE_ISOLATED_EXECUTION`, and docker/boxlite/e2b modes fail closed
rather than fallback to the host browser.

Only exact allowlisted origins are accepted (scheme, hostname and effective
port); HTTPS required except for explicit trusted test/development loopback
with `AGENT_BROWSER_ALLOW_LOOPBACK=true`. No wildcard hosts, credentials in
URL or private DNS targets. Every document/subresource request is intercepted
by Playwright; service workers and WebSockets are disabled, downloads are not
retained, popups are closed. Browser output text is capped at 12k characters.

## Honest limitations / remaining release blockers

- This is an application-layer allowlist; DNS checks and Playwright routing
  **are not** process/network isolation and do not defeat DNS rebinding or
  browser renderer compromise. Operator-managed network egress proxy/firewall,
  pinned resolved endpoints or isolated browser workers are required before
  hosted/untrusted execution.
- Local browser runs on the operator host. There is no direct desktop input,
  screen sharing, download/upload, screenshot artifact, persistent browser
  profile, headful UI or cross-OS Computer Use adapter in this batch.
- Sync Playwright operations are finite-timeout and owned by a dedicated
  thread. Already-dispatched click/fill cannot be rolled back after a
  cancellation, so every state-changing action requires preapproval and must
  never be automatically retried.
- Strong storage/session ownership and download artifacts still depend on
  the next sandbox-scoped browser and artifact authorization workstreams.
- Hosted/multi-tenant Agent execution remains **NO-GO**.

## Evidence

The `browser-use-real.yml` lane uses actual Chromium with disposable
loopback HTTP servers. A JUnit gate requires 0 skipped/failed tests and
checks exact URL allowlist, private/loopback IP controls, redirect/subresource
denial, fresh click/fill UI grants, cancellation, separate cookie profiles and
browser cleanup. It runs on PRs and main changes. The usual full regression
also runs unit/policy tests without needing the optional Playwright SDK.
