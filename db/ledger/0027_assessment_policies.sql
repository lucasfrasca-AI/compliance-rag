-- The policy versions a findings run sent (#300, ADR-0053 F7, ADR-0034).
--
-- A check's coverage statement says which policies it checked. The findings run sends
-- the tenant's in-force policies as the cached prefix; which versions those were is a
-- fact of the run, so it is recorded WITH the assessment, in the same transaction
-- (`LedgerStore.record_findings`), and never re-derived later from what is in force
-- now (a policy withdrawn since would then vanish from a report that checked it).
--
-- `assessment.policies` is a JSON array of `{instrument_id, version_id, title}`, one
-- per version, with the title the ledger recorded for it at the time (ADR-0049). It is
-- NULL for an assessment recorded before this migration, and for a coverage assessment
-- (`POST /v1/coverage`), which sends no policies; a report of an assessment with NULL
-- says "recorded before policy versions were kept". Nothing here is ever updated.
--
-- Idempotent: every statement is guarded, so the runner (#293) and psql -f both
-- re-run it safely.

BEGIN;

ALTER TABLE assessment ADD COLUMN IF NOT EXISTS policies JSONB;

ALTER TABLE assessment DROP CONSTRAINT IF EXISTS assessment_policies;
ALTER TABLE assessment ADD CONSTRAINT assessment_policies
    CHECK (policies IS NULL OR jsonb_typeof(policies) = 'array');

COMMENT ON COLUMN assessment.policies IS
    'the policy versions a findings run sent in its prefix, with their recorded titles (#300, ADR-0053 F7); NULL for an assessment recorded before they were kept, and for a coverage assessment';

COMMIT;
