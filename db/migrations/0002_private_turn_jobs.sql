-- Durable tenant-private turn work ledger, guarded by the same RLS scope.
-- Local Web workers do NOT consume these records; a future authorized hosted
-- worker must explicitly receive a validated tenant delegation before claim.
CREATE TABLE IF NOT EXISTS ridge.private_turn_jobs (
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    turn_id UUID NOT NULL,
    session_id CHAR(12) NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    result JSONB,
    status TEXT NOT NULL DEFAULT 'queued'
       CHECK (status IN ('queued','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    fence BIGINT NOT NULL DEFAULT 0 CHECK (fence >= 0),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, owner_user_id, turn_id),
    UNIQUE (tenant_id, project_id, owner_user_id, idempotency_key),
    FOREIGN KEY (tenant_id, project_id, owner_user_id, session_id)
       REFERENCES ridge.private_sessions (tenant_id, project_id, owner_user_id, session_id)
       ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ridge_private_turns_claim
 ON ridge.private_turn_jobs
 (tenant_id, project_id, owner_user_id, status, lease_until, created_at);

ALTER TABLE ridge.private_turn_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ridge.private_turn_jobs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ridge_private_turns_owner ON ridge.private_turn_jobs;
CREATE POLICY ridge_private_turns_owner ON ridge.private_turn_jobs
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

COMMENT ON TABLE ridge.private_turn_jobs IS
'Infrastructure ledger only: not connected to hosted Agent executor. Lease fencing prevents stale workers from publishing completion.';
