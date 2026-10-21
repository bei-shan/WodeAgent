-- P0: immutable, tenant-private, durable per-session turn-state events.
-- Apply AFTER 0001..0003 as a privileged migration role. Never give the
-- runtime DELETE/UPDATE on the event table. Each transition and cursor
-- allocation is committed in the SAME transaction as the turn ledger.
ALTER TABLE ridge.private_sessions
    ADD COLUMN IF NOT EXISTS turn_event_cursor BIGINT NOT NULL DEFAULT 0
    CHECK (turn_event_cursor >= 0);

CREATE TABLE IF NOT EXISTS ridge.private_turn_events (
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    session_id CHAR(12) NOT NULL,
    event_seq BIGINT NOT NULL CHECK (event_seq > 0),
    turn_id UUID NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued','running','completed','failed','cancelled')),
    fence BIGINT NOT NULL CHECK (fence >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, owner_user_id, session_id, event_seq),
    FOREIGN KEY (tenant_id, project_id, owner_user_id, session_id)
       REFERENCES ridge.private_sessions (tenant_id, project_id, owner_user_id, session_id)
       ON DELETE CASCADE
);
-- No payload/command/result bytes enter the SSE event ledger.
-- A state event must not be used as evidence of external side-effect rollback.

ALTER TABLE ridge.private_turn_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE ridge.private_turn_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ridge_private_turn_event_owner ON ridge.private_turn_events;
CREATE POLICY ridge_private_turn_event_owner ON ridge.private_turn_events
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

COMMENT ON TABLE ridge.private_turn_events IS
'Immutable state-only per-session cursor events, inserted atomically with fenced turn ledger transitions.';
