# HTTP MCP OAuth — local operator bootstrap

This implementation uses the official MCP Python SDK's
`OAuthClientProvider` for **OAuth 2.1** HTTP MCP authorization: protected
resource metadata, authorization server discovery, S256 PKCE, CSRF state,
issuer validation, bearer injection and refresh. OAuth is not used by
stdio servers, which continue to obtain credentials from their environment.

This does **not** change Web login. Ridge's local `WEB_AUTH_TOKEN`
remains the only supported Web authentication mechanism; multi-user OIDC
SSO / RBAC is separate design and deployment work.

## Configuration

In your project `mcp_servers.json`:

```json
{
  "mcpServers": {
    "my-secure-mcp": {
      "transport": "http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "scope": "files:read",
        "callback_port": 8765
      }
    }
  }
}
```

**Important:** The MCP HTTP endpoint must be HTTPS; unencrypted HTTP is
allowed only for explicit loopback hosts. Auth config does not accept
client secrets, redirect URLs, or arbitrary commands.

Run locally on the same machine where Ridge is running:

```bash
python scripts/mcp_oauth_login.py --server my-secure-mcp
```

The CLI starts a temporary listener at
`http://127.0.0.1:8765/callback` and opens the provider's browser login.
Register **that exact callback URI** with the authorization server where
pre-registration is required. If it cannot register a dynamic client or
support S256 PKCE, the SDK fails the authentication flow. The listener
requires a state-matching callback, accepts a one-use authorization code,
and shuts down on success, timeout or cancellation. The SDK performs token
exchange and refresh; Ridge never sees the password.

**Automatic Agent startup never launches a browser.** If no authorized
tokens are cached, the OAuth MCP connection fails closed with an
instruction to use the explicit CLI. Already authorized credentials can
be reused and refreshed by the SDK on later connections.

## Token protection

Cache path: `.mycodeagent/oauth/<sha256-of-endpoint-prefix>.json`.
Tokens and OAuth client registration are kept across restarts so refresh
does not require another login every time. On POSIX the parent directory
uses mode `0700`, the file uses `0600`, and writes use atomic replace.
Symlinked token files/directories are rejected.

**The token cache is not encrypted at rest.** This is intended for a
trusted local single-operator machine with suitable filesystem
permissions. Keep `.mycodeagent/` out of git, backups that are shared
publicly, and diagnostics. Do not use this as a multi-tenant credentials
store or expose the MCP auth bootstrap through a public Web endpoint.
If a provider authorization URL is displayed in the terminal, do not
include it in bug reports or command logs.

## Operational limitations

- The Python MCP SDK performs discovery, PKCE and refresh; Ridge does not
  implement a replacement OAuth protocol or token validation server.
- The flow requires the **same workstation** for browser redirect and
  callback, with the chosen port free. Headless server, cloud and remote
  desktop setups require a separate authorization design.
- This is an opt-in HTTP transport authentication path, not an inbound
  authentication server for Ridge Web.
- Revoking credentials at the OAuth authorization server and deleting the
  relevant local token cache may be necessary to revoke an MCP connection.
- Client-side request cancellation never guarantees rollback of remote
  side effects.

## Verification

`tests/test_mcp_oauth_local.py` checks allowlisted HTTPS/loopback
resources, no OAuth on stdio, SDK OAuth provider construction, token
storage permissions and atomicity, disallowed symlinks, one-shot callback
state filtering, and cleanup on cancellation. The tests also run in the
MCP 1.x compatibility lane. Live external OAuth provider acceptance
is a **manual release gate**, not covered by these offline tests.
