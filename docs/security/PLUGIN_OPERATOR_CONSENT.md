# P1 Plugin Hook operator consent hardening (2026-10-22)

## Why an unkeyed fingerprint was not sufficient

A plugin can contain executable shell hooks. The former local project state
`.mycodeagent/plugins/state.json` stored both `trusted=true` and the expected
SHA-256. A project writer could modify plugin files AND recompute the digest
in this same writable directory. An unkeyed hash proves matching bytes, not
**operator authorization**.

## New fail-closed executable Hook consent

Code-bearing plugin approval requires all of the following:

1. The plugin was enabled intentionally; `RIDGE_ALLOW_TRUSTED_HOOKS=1` is
   operator-set before session construction.
2. A reviewed manifest/assets tree matches the bounded local SHA-256.
3. A keyed HMAC consent receipt covers the plugin name, exact canonical
   project root and reviewed digest; the key is **NOT** in project content.
4. The private key file is a **32-byte** owner-only regular file at
   `~/.config/ridgecode/plugin-approval.key`, with POSIX `0600`
   permissions, one hard link and owner-controlled non-group-writable
   configuration directories.
5. Any missing, too-short, too-permissive, modified or relocated key
   means no command Hook execution. Project-provided JSON cannot mint an
   approval receipt. No insecure environment-variable bypass exists.

The operator provisions the key **outside the untrusted repository**, for
example with a secure key generator while under a trusted account; use a
platform secrets manager to protect backups and rotation. On first consent,
an operator must review the Hook shell commands and all project assets then
explicitly run `PluginCatalog.set_trusted(name, True)`. After rotation,
consent must be reviewed and signed again. This gate does not automatically
create keys, enable plugins or execute Hook code.

Windows code Hooks are **fail-closed** until a separate Windows-native,
ACL-audited operator key store has been implemented; Windows host process
tree handling alone is not a general code signing mechanism.

## Residual blockers and required next steps

- HMAC consent does **not** freeze filesystem bytes after plugin discovery.
  Mutable repositories and shell Hook entrypoints still admit check/use races.
  To use Hooks with hostile projects, move their executable assets to a
  sealed, immutable snapshot or agent-specific out-of-tree store and verify
  before **every** execution.
- Shell Hooks are trusted host code with authority of the local user, not
  sandboxed plugins. Hosted/isolation modes do not grant arbitrary host
  command Hook execution.
- No third-party publisher signature, package transparency log, independent
  supply-chain attestation or signed binary distribution has yet been
  implemented; source pinning and local operator consent are distinct.
- Test `tests/test_plugin_trust_integrity.py` includes signed approval,
  mutated assets, forged state/digest, key permission changes and receipt
  replay across workspaces. All Python regression checks must pass.

This closes the **mutable state forgery** problem for executable Hook
approval; it does not constitute a complete secure plugin marketplace.
