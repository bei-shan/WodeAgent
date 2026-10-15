-- Ridge Code: first private tenant-scoped session table (PostgreSQL 15+).
-- Run as a migration/admin role. Runtime role MUST be non-superuser without
-- BYPASSRLS and without unrestricted DDL privileges. Never use the migration
-- owner as the application user.
CREATE SCHEMA IF NOT EXISTS ridge;

CREATE TABLE IF NOT EXISTS ridge.private_sessions (
    tenant_id      TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    owner_user_id  TEXT NOT NULL,
    session_id     CHAR(12) NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    snapshot       JSONB NOT NULL DEFAULT '{}'::jsonb,
    version        BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, owner_user_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_ridge_private_sessions_recent
    ON ridge.private_sessions (
        tenant_id, project_id, owner_user_id, updated_at DESC
    );

ALTER TABLE ridge.private_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE ridge.private_sessions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ridge_private_sessions_owner ON ridge.private_sessions;
CREATE POLICY ridge_private_sessions_owner ON ridge.private_sessions
    FOR ALL
    USING (
        tenant_id = nullif(current_setting('ridge.tenant_id', true), '')
        AND project_id = nullif(current_setting('ridge.project_id', true), '')
        AND owner_user_id = nullif(current_setting('ridge.user_id', true), '')
    )
    WITH CHECK (
        tenant_id = nullif(current_setting('ridge.tenant_id', true), '')
        AND project_id = nullif(current_setting('ridge.project_id', true), '')
        AND owner_user_id = nullif(current_setting('ridge.user_id', true), '')
    );

COMMENT ON TABLE ridge.private_sessions IS
'Private sessions only. Caller must establish verified principal and SET LOCAL scope per transaction. No team sharing, public web routes or superuser connections.';
