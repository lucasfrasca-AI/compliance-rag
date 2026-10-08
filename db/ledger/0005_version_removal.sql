-- Version-and-retire and the three removal states (ADR-0021, D-18).
--
-- Withdraw and purge mark the instrument_version row; neither ever deletes it.
-- Every withdraw / purge / replace-in-place is audited whatever its outcome.
-- count_findings_citing() is the exact query the purge guardrail runs, kept in
-- the database so it is proven against real Postgres here rather than first
-- appearing inside an adapter. Mirrors FakeLedgerStore.count_findings_citing.

BEGIN;

ALTER TABLE instrument_version
    ADD COLUMN withdrawn_at TIMESTAMPTZ,
    ADD COLUMN withdrawn_by TEXT,
    ADD COLUMN purged_at    TIMESTAMPTZ,
    ADD COLUMN purged_by    TEXT,
    ADD COLUMN purge_reason TEXT,
    ADD CONSTRAINT instrument_version_withdrawn_complete CHECK (
        (withdrawn_at IS NULL) = (withdrawn_by IS NULL)
    ),
    ADD CONSTRAINT instrument_version_purged_complete CHECK (
        (purged_at IS NULL) = (purged_by IS NULL)
        AND (purged_at IS NULL) = (purge_reason IS NULL)
    ),
    -- Library law is unsubscribed from, never withdrawn or purged. Unreachable
    -- today (issue #67: library law can't be inserted here yet); this keeps it
    -- true once it can.
    ADD CONSTRAINT instrument_version_removal_is_tenant_owned CHECK (
        (withdrawn_at IS NULL AND purged_at IS NULL) OR tenant_id <> '_public'
    );

-- No foreign key to tenant: an audit row must be writable for a refused
-- request too, including one naming `_public` or a tenant that doesn't exist.
CREATE TABLE IF NOT EXISTS corpus_removal_audit (
    audit_id        TEXT        PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    instrument_id   TEXT        NOT NULL,
    version_id      TEXT,
    action          TEXT        NOT NULL
                    CHECK (action IN ('withdraw', 'purge', 'replace_in_place')),
    outcome         TEXT        NOT NULL
                    CHECK (outcome IN ('refused', 'authorized', 'completed', 'failed')),
    actor           TEXT        NOT NULL,
    reason          TEXT        NOT NULL,
    findings_citing INTEGER     CHECK (findings_citing IS NULL OR findings_citing >= 0),
    detail          TEXT        NOT NULL DEFAULT '',
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS corpus_removal_audit_by_target
    ON corpus_removal_audit (tenant_id, instrument_id, version_id);

-- starts_with, not LIKE: '_' is a LIKE wildcard and is legal in instrument ids.
CREATE OR REPLACE FUNCTION count_findings_citing(p_tenant_id TEXT, p_stable_id_prefix TEXT)
RETURNS INTEGER
LANGUAGE sql STABLE AS $$
    SELECT count(*)::INTEGER
    FROM finding f
    WHERE f.tenant_id = p_tenant_id
      AND EXISTS (
          SELECT 1
          FROM jsonb_array_elements(f.citations) AS c
          WHERE starts_with(c ->> 'clause_id', p_stable_id_prefix)
      )
$$;

COMMIT;
