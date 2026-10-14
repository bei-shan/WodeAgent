# Project Plugins and Skills (v0.1)

This phase implements a **local-first project plugin lifecycle**, taking
the deterministic discovery idea from [OpenCode's plugin architecture](
https://opencode.ai/docs/plugins/) and the declarative
`.claude-plugin/plugin.json` / `skills/<name>/SKILL.md` layout from
Claude Code. It deliberately does **not** execute arbitrary JS/TS, fetch
npm packages, clone untrusted Git URLs, or auto-install executable code.

## Existing findings (before this change)

- `SkillLoader` loaded bundled `skills/` and writable
  `.mycodeagent/skills/`, but seeded only SKILL.md, losing references,
  scripts and assets from the runtime copy.
- `core/plugin_loader.py` existed, but had **no installation or enable
  state**. Its declared Skills directories were not registered with
  SkillLoader; plugin hook initialization **rewrote** the project's
  `.mycode/hooks.json`; Python custom features could import at discovery.
- When Web rebound an Agent to `.mycodeagent/sessions/<id>` for tool
  isolation, the Agent replaced its project SkillLoader with one targeting
  the session workspace, so the Web skill catalog and live Agent could
  disagree on visible Skills.

## Plugin layout

Import a local ZIP with its manifest at **archive root**, containing
`plugin.json` or `.claude-plugin/plugin.json`. For example:

```text
plugin.json
skills/
  review/
    SKILL.md
    references/checklist.md
feature.py                    # optional code; stays inert by default
```

```json
{
  "name": "review-kit",
  "version": "0.1.0",
  "description": "Reviews source code",
  "features": {
    "skills": ["skills"],
    "hooks": {},
    "custom_features": []
  }
}
```

`name` must be kebab-case. Component paths must be relative with no
traversal or symlinks. Skills use frontmatter with `name` and
`description`, and are *loaded on demand* by the standard `Skill` tool.
The standard `Skill` tool also supports
`{"action":"read_resource","name":"review","resource":"references/checklist.md"}`
for on-demand, text-only bundled reference access. It rejects absolute and
parent paths, symlinks, unsupported types and files over 128 KiB. This
allows isolated Web sessions to use reference material without broadening
the ordinary workspace Read tool's filesystem permissions.

The plugin's Skill resources remain at their original plugin folder (not
injected into a writable runtime copy). Project runtime Skills override
source/plugin names; built-in source Skills override plugin names.

## Install, enable, trust, remove

Web sidebar → **插件** provides local ZIP upload, list, enable/disable,
explicit code trust and uninstall. Equivalent authenticated local APIs:

- `GET /api/plugins`
- `POST /api/plugins/install` multipart with a ZIP under 2 MiB
- `PUT /api/plugins/{name}/enabled` with `{"enabled":true}`
- `PUT /api/plugins/{name}/trusted` with `{"trusted":true}`
- `DELETE /api/plugins/{name}`

Installation copies to `.mycode/plugins/<name>` using temporary
same-filesystem staging. Total extracted data is capped at 6 MiB and
128 files. ZIP path traversal, symbolic links, duplicate names and
unsafe component paths are rejected. Untrusted archives are never
executed during installation. There is no remote download, marketplace
auto-install or package dependency resolver in this phase.

All newly installed and previously present plugins start **disabled**
unless explicitly enabled. State lives in
`.mycodeagent/plugins/state.json` and is written by atomic rename.

An **enabled** plugin contributes its declared Skills. Its Python features
and command Hooks remain inert unless **both** (1) the user explicitly
sets `trusted=true` and (2) the operator sets
`RIDGE_ALLOW_PLUGIN_CODE=1` on the backend. Such code is still arbitrary
host-level code in the current process and **is not sandboxed**.
Only enable the code gate for reviewed, locally trusted plugins.
The trust decision is now bound to a SHA-256 fingerprint of the installed
manifest, declared source files and other non-bytecode plugin resources. If
a file is edited, added or replaced after consent, the next plugin discovery
fails closed even when the old `trusted=true` flag remains set. The Web UI
shows the stale approval state; disable trust, review the changed content,
then explicitly trust again. Legacy unbound trust flags also fail closed.
Generated `__pycache__` bytecode is not part of the fingerprint.

**This is an integrity check, not an OS filesystem protection or a
sandbox.** An already initialized Agent feature can remain in memory,
and a malicious local actor with write access to the project can race
file checks. Recreate sessions when revoking trust; for unreviewed code,
leave `RIDGE_ALLOW_PLUGIN_CODE` disabled. Plugin-defined capability lists
would be only advisory until executable extensions run in isolated workers.

No hook config is written into `.mycode/hooks.json`; trusted hooks are
registered in memory. Code components are selected at Agent creation;
create a new session or restart the Agent after changing code trust.
Skill enabled state is re-scanned when requested and on Agent prompt refresh.

## Version provenance, updates, pinned integrity and rollback

The Web plugin panel now displays the install origin (`local_zip` or
`local_directory`) and the SHA-256 of the original imported ZIP. Operators
can lock a ZIP installation, which blocks subsequent updates until it is
explicitly unpinned. Updated ZIPs can be sent to
`POST /api/plugins/{name}/update?sha256=<expected SHA-256>`.
The optional expected hash rejects a replacement archive with a
different digest **before any directory changes**.

Updating validates the full archive in isolated temporary staging, rejects
a different plugin name or unchanged manifest version, retains one prior
version in project-private backups, and switches the active plugin directory
through same-filesystem renames. A failed publish restores the prior
version. On every update or rollback, **enabled=false** and
**trusted=false** regardless of previous state. Create a new Agent session
after updating code plugins; already initialized in-process code cannot
be forcibly unloaded from a running turn.

`PUT /api/plugins/{name}/pinned` accepts `{"pinned":true|false}`.
`POST /api/plugins/{name}/rollback` restores the single retained prior
revision. `DELETE /api/plugins/{name}` deletes this backup too.

**SHA-256 verifies archive integrity, not publisher authenticity.** There
are no remote marketplaces, automatic pulls, developer signatures, binary
attestations, or isolated execution for trusted code in v0.1. Do not enable
unreviewed third-party Python/Hook components; future code isolation will
require a separate process/container boundary.

## Separation from MCP and limitations

MCP server management remains separate: an MCP tool is an external
protocol endpoint and keeps its JSON Schema, permission and cancellation
boundaries. Plugins package local skills/hooks/optional feature code.
This release does not yet bundle MCP servers, register plugin-defined tool
schemas automatically, implement signed packages or solve multi-tenant
plugin trust. The Web service must remain local-only or protected by
its configured authentication; **plugin management is not safe as an
unauthenticated public upload endpoint**.

The legacy Web Skills enabled switch writes `skill_state.json`. The
Agent's SkillLoader now honors these disable flags on refresh and when the
Skill tool loads a named skill. The Skill tool's `create` action is routed
through the shared SkillLoader with atomic writes, instead of silently
writing to an isolated Web session workspace where the shared catalog
cannot see it.

An independent Agent Teams regression also closes a race in `TeamStore`:
work-item JSONL snapshots are written to unique temporary files and
atomically replaced while holding the per-owner writer lock. Concurrent
readers now see complete work-item records, not half-written JSON lines.

Existing bundled source Skills now seed **complete** resource directories
with symlinks skipped, keeping references and scripts intact. The Web
Agent keeps the shared project Skill/Plugin catalog across session
workspace rebinding, while file tools still operate inside the isolated
workspace.

## Verification

`tests/test_plugin_lifecycle.py` covers install/enable/disable/uninstall,
Skills discovery, bundled assets, untrusted Python code never executing,
dual trust gating, in-memory hook registration, ZIP traversal/symlinks/
size/manifest checks and real authenticated-stack API behavior.
