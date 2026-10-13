# Project Skills management service

Skills CRUD and validation now belong to `desktop.service.application.SkillCatalogService`.
FastAPI only marshals request and response schemas and maps ServiceError.

Security boundaries:
- Creates and edits only under `.mycodeagent/skills/<name>/SKILL.md`.
- Built-in `skills/` is a read-only reference, never deleted through the API.
- Rejects invalid kebab-case names and frontmatter newline injection.
- Loader refuses symlinked or project-external SKILL.md paths, including
  symlinked ancestor directories, before reading or seeding defaults.
- Updates replace a fully written temporary file atomically, with an
  in-process lock for concurrent create/update/delete commands.

Scope note: this is a single-user local project. Path checks are not a
substitute for descriptor-anchored directory handles when an adversarial
local process concurrently modifies parent directories.

Regression tests cover list/read/create/update/delete, source read-only
semantics, symlink targets and parents, malicious names and concurrent create.
