# Verified hosted tool execution fence — 2026-10-15

## Security boundary

The hosted API at `desktop/service/hosted_app.py` **remains metadata-only**.
It does not expose any new HTTP Agent/LLM/tool routes. The existing local
Agent API is explicitly refused in hosted deployment mode.

The new capability fence is a Worker-side prerequisite, not an authorization
claim based solely on a model prompt or an OIDC token.

1. Trusted server code calls `OIDCVerifier.verify` then supplies its
   `HostedIdentity` and authoritative membership store to
   `trusted_hosted_tool_authority(identity, store)`. Never derive the scope
   from request headers, model arguments or serialized session snapshots.
2. For a future isolated Worker, create `CodeAgent(...,
   hosted_tool_authority=authority)` with `sandbox_mode=docker|boxlite|e2b`.
   `local_trusted` is refused. The existing sandbox registry is sealed to
   isolated `Bash` before the authority binds.
3. `ToolRegistry` checks the immutable tenant/user/project scope and calls
   `store.authorize_membership(scope)` on **each** tool-schema lookup and
   dispatch. A denial, raised exception or unavailable membership store
   removes the schema and returns `PERMISSION_DENIED` before invoking any
   adapter. The authorization is independently checked for direct registry
   calls, not merely model-visible descriptions.
4. A hosted registry cannot rebind to a different principal. Derived Task
   registries inherit the original sealed authority and isolated-tool
   restriction. Hosted Agent code forbids a custom `_execute_tool` override
   from bypassing the canonical dispatcher. The previous per-model-step
   adapter/schema seal remains in force.
5. Hosted mode currently permits **only isolated Bash**. MCP, plugins,
   interactive approvals, Task, team, host filesystem, and all other
   capabilities are denied pending resource-level authorization contracts.

## Verification

`tests/test_hosted_tool_authorization.py` covers equal user IDs in distinct
tenants, mutable membership revocation, membership DB exceptions, direct
registry calls, model snapshots, concurrent calls across registries, derived
subagent registries and real CodeAgent initialisation with a mock Docker
command runner. Existing Web API/metadata tests still verify no hosted Agent
routes were exposed.

## Not a production hosted Agent service yet

The next release gates remain a durable identity-bound worker queue and
database fencing leases; per-tenant, per-project sandbox creation and
credential isolation; explicit scoped MCP/plugin/resource policies; audit
and cancellation fencing; secret scanning; and real multi-tenant staging
isolation/concurrency/chaos testing. This implementation must not be used
to enable the local trusted API on a multi-tenant host.
