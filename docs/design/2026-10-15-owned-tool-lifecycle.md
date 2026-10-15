# Tool registration ownership and MCP lifecycle — 2026-10-15

## Motivation

Even with a per-model-step tool seal, a registry needs an authoritative source
for each adapter. Without it a plugin can replace a built-in name by mistake,
and MCP server retries may create numbered duplicates rather than replacing
previous registrations.

## Implementation

- `ToolRegistry` records one source owner for every object/function tool,
  defaulting to `legacy` for old call sites.
- Different owners cannot replace/unregister each other's tools. The
  `builtin`, `team`, and `mcp:<server_name>` labels are set at their
  registration entrypoints; the isolated sandbox Bash override uses
  `builtin` because it intentionally replaces the built-in Bash adapter.
- `replace_owned_tools(owner, adapters)` checks duplicates and all name
  collisions **before** making changes. Once validated it atomically
  replaces only the given owner's inventory under the registry lock.
  Discovery approvals for replaced/deleted adapters are revoked.
- An MCP reconnect first completes remote tool discovery and constructs its
  candidate adapters. Only then does it publish a complete new inventory,
  dropping remote tools deleted from that server. MCP sanitization reuses
  stable names for registrations owned by the *same* server and suffixes
  collisions owned by different sources.
- MCP feature cleanup removes MCP-owned registrations before closing clients,
  without deleting team, built-in or plugin registrations.

## Boundaries and limitations

Owner labels are registration **provenance**, not a security principal: any
Python code already running with direct registry object access can invoke
administrative APIs. Hosted user/tenant claims must be verified by a separate
authorization layer at every execution entrypoint. In-flight tool calls that
already crossed the dispatch boundary are not forcibly terminated.
`get_tool` is an internal compatibility accessor and does not represent
a tool capability grant; the model dispatch path uses the sealed ToolRegistry.

Validation includes owner collision rollback, reconnection stable names,
cross-MCP-server sanitization collisions, scoped cleanup and stale step
snapshots, plus all existing CI suites.
