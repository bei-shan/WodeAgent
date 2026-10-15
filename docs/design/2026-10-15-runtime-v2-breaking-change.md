# Ridge runtime v2: no AgentFeature compatibility layer

Date: 2026-10-15. Breaking change.

## Current architecture

Agent execution uses framework-owned `RuntimeServices` and mandatory
`PolicyMiddlewarePipeline`. Worktree, MCP, Team, Background, Style, Hook,
VCR, local Session and isolated Sandbox are runtime-owned services. Startup
is explicit; the service graph validates dependencies, unwinds partial
failures and shuts down in reverse order. Post-start actions run only
after built-in tool registration.

The old `core/features` package, `AgentFeature` superclass, numeric
feature registry, feature teardown adapters and auto-loaded Python
`custom_features` extension entrypoints have been **deleted**. Importing
`core.features` is an error rather than silently relying on a shim.

Runtime prompt contributions are drawn from named, typed services and
policies. The ToolRuntime verifies mandatory Plan/Budget/Delegate limits
**before** optional declarative tool hooks. Model interception is owned
by VCRService, not by a generic privileged plugin callback.

## Plugin contract

Plugin manifests may contribute Skills, output styles and explicitly
consented declarative Hooks. PluginLoader does not `exec_module` any
project-provided Python, and invalid `custom_features` manifests are
rejected during installation or discovery. The deprecated
`RIDGE_ALLOW_PLUGIN_CODE` flag grants no capabilities. Explicit command
Hook consent requires `RIDGE_ALLOW_TRUSTED_HOOKS=1`; operators must port to a constrained tool/
hook contribution with explicit capability grants. Trusted local shell
Hooks continue to require operator opt-in; they are never loaded in
hosted identity mode.

## Not yet production-certified

This change removes the obsolete extension ABI and reduces host-side
attack surface; it does **not** constitute readiness for hosted execution.
The hosted HTTP API stays metadata-only. Authenticated fenced worker
integration, per-tenant filesystem+credential+compute isolation,
external MCP resource grants, cancellation proof, distributed chaos
tests, performance SLOs and a release rollback plan are still required.
Do not interpret passing unit/CI tests as multi-tenant staging acceptance.
