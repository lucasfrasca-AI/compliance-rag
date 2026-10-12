-- Profile facts as dated evidence (issue #114, the #93 design session, Q2).
--
-- 0001 keyed evidence to a condition (`condition_id`) with a `satisfied`
-- flag. But one fact feeds many conditions (annual turnover feeds s6D(1),
-- s6D(2) and s6D(4)(a)), and whether a condition is satisfied is computed
-- per assessment, not stored. So evidence is now about a *field* for a
-- *period*: `annual_turnover_aud` was 2,500,000 for FY2025-26, on the CFO's
-- word. Nothing has ever written the old shape, so the table is reshaped in
-- place.
--
-- Append-only: a correction is a new row naming the row it `supersedes`
-- (one successor each); nothing is updated.

BEGIN;

ALTER TABLE evidence DROP COLUMN IF EXISTS condition_id;
ALTER TABLE evidence DROP COLUMN IF EXISTS satisfied;

ALTER TABLE evidence ADD COLUMN IF NOT EXISTS field        TEXT NOT NULL;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS period_start DATE NOT NULL;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS period_end   DATE NOT NULL;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS source       TEXT NOT NULL;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS supersedes   TEXT;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS trace_id     TEXT;

ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_period;
ALTER TABLE evidence ADD CONSTRAINT evidence_period CHECK (period_end > period_start);

ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_field;
ALTER TABLE evidence ADD CONSTRAINT evidence_field CHECK (field ~ '^[a-z][a-z0-9_]*$');

ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_source;
ALTER TABLE evidence ADD CONSTRAINT evidence_source CHECK (btrim(source) <> '');

-- Documents inform, humans assert: an assertion names who made it.
ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_asserted_by;
ALTER TABLE evidence ADD CONSTRAINT evidence_asserted_by
    CHECK (provenance_tier <> 'asserted' OR asserted_by IS NOT NULL);

ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_supersedes_other;
ALTER TABLE evidence ADD CONSTRAINT evidence_supersedes_other CHECK (supersedes <> evidence_id);

ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_supersedes_fk;
ALTER TABLE evidence ADD CONSTRAINT evidence_supersedes_fk
    FOREIGN KEY (tenant_id, supersedes) REFERENCES evidence (tenant_id, evidence_id);

ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_one_successor;
ALTER TABLE evidence ADD CONSTRAINT evidence_one_successor UNIQUE (tenant_id, supersedes);

CREATE INDEX IF NOT EXISTS evidence_by_field ON evidence (tenant_id, field);

COMMIT;
