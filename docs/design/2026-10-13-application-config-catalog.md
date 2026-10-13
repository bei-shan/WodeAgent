# Project Configuration Catalog extraction

## Architecture

FastAPI now only maps HTTP request/response DTOs for persisted Skill/MCP
enable flags, MCP server configuration CRUD, and Hooks JSON. State ownership
moves to desktop.service.application.config_catalog.ConfigCatalogService.

Dependency path:
  HTTP routes -> ConfigCatalogService -> project-local JSON files

This service is framework-independent. It serializes read/modify/write under
a service-level reentrant lock and uses atomic same-directory replacement so
concurrent toggles don't overwrite each other or produce half-written JSON.

## Compatibility

Public route paths, status codes (201/404/409) and JSON success envelopes are
unchanged. In-process locks do not coordinate *multiple* backend processes;
future deployments that enable multi-process writes need SQLite or locking at
storage level. These configuration files are not a multi-tenant settings store.

## Next

Extract Skill CRUD into SkillService with explicit name/path validation and
separate storage ownership. TeamManager requires a separate state-ownership
audit before moving worker lifecycle logic. Leave old Python TUI intact until
TS TUI feature parity and interactive PTY acceptance are demonstrated.
