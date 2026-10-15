-- Database-authoritative project membership for the isolated hosted API.
-- A signed OIDC scope establishes identity, but access is granted only while
-- this row remains enabled. IdP/admin provisioning owns inserts and revocation.
-- Run as migration role; grant runtime role SELECT only, NEVER mutation.
CREATE TABLE IF NOT EXISTS ridge.private_project_memberships (
    tenant_id    TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT false,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_ridge_project_membership_enabled
 ON ridge.private_project_memberships (tenant_id, project_id, user_id)
 WHERE enabled;

ALTER TABLE ridge.private_project_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE ridge.private_project_memberships FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ridge_membership_self_read ON ridge.private_project_memberships;
CREATE POLICY ridge_membership_self_read ON ridge.private_project_memberships
    FOR SELECT
    USING (
      tenant_id = nullif(current_setting('ridge.tenant_id', true), '')
      AND project_id = nullif(current_setting('ridge.project_id', true), '')
      AND user_id = nullif(current_setting('ridge.user_id', true), '')
    );

COMMENT ON TABLE ridge.private_project_memberships IS
'Authoritative current project membership; only privileged provisioning may modify. Runtime service SELECT only, scoped by forced RLS.';
