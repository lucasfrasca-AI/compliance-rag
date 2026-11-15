-- Findings of a check (#299, ADR-0053 F1-F6, ADR-0059 Q6 and C1).
--
-- A check's findings run is an assessment (`assessment`, with the corpus and
-- mapping commits it was computed against, 0013), its coverage (`coverage`,
-- 0010), and one `finding` per claim and obligation (law) or claim and policy
-- clause (policy), all written in ONE transaction by the adapter. Nothing here
-- is ever updated: an assessment keeps what it said (ADR-0034), so a check run
-- again after the facts changed is a new assessment beside it.
--
-- What this adds to ledger schema v1's `assessment` and `finding`:
--
-- * `assessment.check_id`: the check the assessment judged (0024). One
--   assessment per check.
-- * `finding.check_id` + `finding.claim_index`: the claim (0025), by number.
--   **The ledger does not carry the document's words**, so `claim_text`, which
--   was NOT NULL, is optional for a finding that names its claim this way.
-- * `finding.verdict` is optional, and `finding.outcome` says what a row is:
--   `verdict` (the model's, every citation verified), `conditional` (against an
--   obligation Track A can't decide: built by code, no verdict, F4), `withheld`
--   (a citation was blocked or it cited nothing: the verdict is NOT STORED, so
--   it can never be shown, ADR-0003) or `failed` (no finding could be made).
-- * `finding.settle_by`: what would settle an `ambiguous` verdict, required for
--   it and only for it (F3).
-- * `finding.outcome_detail`: our word for why a finding is withheld or failed.
-- * `finding.blocked`: the clauses whose citations blocked a withheld finding,
--   with our word for why, never the model's quote. Kept apart from `citations`
--   so that a blocked clause never counts as one a finding cites
--   (`count_findings_citing`, 0005 and 0015).
-- * `finding.derived_from`: the finding a derived finding comes from through an
--   approved policy mapping (F6, #306). Nothing writes it yet.
--
-- `check_findings_run` is the reservation, as `check_extraction` is for the
-- extraction: one run per check, reserved before any model is called, so a
-- double click or a retry spends no second call.
--
-- Idempotent: every statement is guarded, so the runner (#293) and psql -f both
-- re-run it safely.

BEGIN;

ALTER TABLE assessment ADD COLUMN IF NOT EXISTS check_id TEXT;

ALTER TABLE assessment DROP CONSTRAINT IF EXISTS assessment_check_fk;
ALTER TABLE assessment ADD CONSTRAINT assessment_check_fk
    FOREIGN KEY (tenant_id, check_id) REFERENCES submitted_check (tenant_id, check_id);

ALTER TABLE assessment DROP CONSTRAINT IF EXISTS assessment_check_id;
ALTER TABLE assessment ADD CONSTRAINT assessment_check_id
    CHECK (check_id IS NULL OR btrim(check_id) <> '');

CREATE UNIQUE INDEX IF NOT EXISTS assessment_one_per_check
    ON assessment (tenant_id, check_id) WHERE check_id IS NOT NULL;

ALTER TABLE finding ADD COLUMN IF NOT EXISTS check_id TEXT;
ALTER TABLE finding ADD COLUMN IF NOT EXISTS claim_index INTEGER;
ALTER TABLE finding ADD COLUMN IF NOT EXISTS settle_by TEXT;
ALTER TABLE finding ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'verdict';
ALTER TABLE finding ADD COLUMN IF NOT EXISTS outcome_detail TEXT;
ALTER TABLE finding ADD COLUMN IF NOT EXISTS blocked JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE finding ADD COLUMN IF NOT EXISTS derived_from TEXT;

ALTER TABLE finding ALTER COLUMN claim_text DROP NOT NULL;
ALTER TABLE finding ALTER COLUMN verdict DROP NOT NULL;

ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_claim_fk;
ALTER TABLE finding ADD CONSTRAINT finding_claim_fk
    FOREIGN KEY (tenant_id, check_id, claim_index)
    REFERENCES check_claim (tenant_id, check_id, claim_index);

ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_derived_from_fk;
ALTER TABLE finding ADD CONSTRAINT finding_derived_from_fk
    FOREIGN KEY (tenant_id, derived_from) REFERENCES finding (tenant_id, finding_id);

ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_outcome;
ALTER TABLE finding ADD CONSTRAINT finding_outcome
    CHECK (outcome IN ('verdict', 'conditional', 'withheld', 'failed'));

-- A claim is named by its number, or by its words (a finding recorded before
-- documents were checked), and the two numbers go together.
ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_names_its_claim;
ALTER TABLE finding ADD CONSTRAINT finding_names_its_claim
    CHECK ((check_id IS NULL) = (claim_index IS NULL)
           AND (claim_text IS NOT NULL OR check_id IS NOT NULL));

-- Only a verdict row has a verdict: a withheld finding's is never stored.
ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_verdict_iff_verdict;
ALTER TABLE finding ADD CONSTRAINT finding_verdict_iff_verdict
    CHECK ((outcome = 'verdict') = (verdict IS NOT NULL));

-- F3: an `ambiguous` finding names what would settle it, and no other does.
ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_settle_by_iff_ambiguous;
ALTER TABLE finding ADD CONSTRAINT finding_settle_by_iff_ambiguous
    CHECK ((verdict = 'ambiguous' AND settle_by IS NOT NULL AND btrim(settle_by) <> ''
            AND settle_by !~ E'[\\n\\r]')
           OR (verdict IS DISTINCT FROM 'ambiguous' AND settle_by IS NULL));

-- F4: conditional is the same fact as not assessable.
ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_conditional_iff_unassessable;
ALTER TABLE finding ADD CONSTRAINT finding_conditional_iff_unassessable
    CHECK ((outcome = 'conditional') = (NOT assessable));

-- Our word for why, on a withheld or failed row only.
ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_detail_iff_withheld_or_failed;
ALTER TABLE finding ADD CONSTRAINT finding_detail_iff_withheld_or_failed
    CHECK ((outcome IN ('withheld', 'failed')) = (outcome_detail IS NOT NULL));

-- A finding of a check is against one obligation or one policy clause (F1).
ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_one_target;
ALTER TABLE finding ADD CONSTRAINT finding_one_target
    CHECK (check_id IS NULL OR ((obligation_id IS NULL) <> (policy_clause_id IS NULL)));

-- A finding of a check shows citations only with a verdict, and always cites
-- with one (the verifier checked each, ADR-0003); a blocked one is named in
-- `blocked`, on a withheld row alone.
ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_citations_iff_verdict;
ALTER TABLE finding ADD CONSTRAINT finding_citations_iff_verdict
    CHECK (check_id IS NULL
           OR ((outcome = 'verdict') = (jsonb_array_length(citations) > 0)));

ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_blocked_only_withheld;
ALTER TABLE finding ADD CONSTRAINT finding_blocked_only_withheld
    CHECK (outcome = 'withheld' OR jsonb_array_length(blocked) = 0);

-- One finding per claim and target, within an assessment.
CREATE UNIQUE INDEX IF NOT EXISTS finding_one_per_claim_and_target
    ON finding (tenant_id, assessment_id, claim_index,
                COALESCE(obligation_id, ''), COALESCE(policy_clause_id, ''))
    WHERE check_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS finding_by_claim ON finding (tenant_id, check_id, claim_index);

CREATE TABLE IF NOT EXISTS check_findings_run (
    tenant_id    TEXT        NOT NULL,
    check_id     TEXT        NOT NULL,
    trace_id     TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending',
    reserved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, check_id),
    FOREIGN KEY (tenant_id, check_id) REFERENCES check_extraction (tenant_id, check_id),
    CONSTRAINT check_findings_run_trace_id CHECK (btrim(trace_id) <> ''),
    CONSTRAINT check_findings_run_status CHECK (status IN ('pending', 'done', 'failed')),
    CONSTRAINT check_findings_run_completed CHECK ((status = 'done') = (completed_at IS NOT NULL))
);

COMMENT ON TABLE check_findings_run IS
    'the reservation of one check''s findings run, once per check (#299): pending, then done with its assessment, or failed';
COMMENT ON COLUMN assessment.check_id IS
    'the check this assessment judged (#299), NULL for a coverage assessment';
COMMENT ON COLUMN finding.claim_index IS
    'the claim of the check''s extraction (check_claim); the ledger does not carry the document''s words (#298, #299)';
COMMENT ON COLUMN finding.outcome IS
    'verdict, conditional (Track A can''t decide the obligation, F4), withheld (a citation blocked: no verdict is stored) or failed';
COMMENT ON COLUMN finding.settle_by IS
    'what would settle an ambiguous verdict (F3): the model''s text, required for ambiguous and only for it';
COMMENT ON COLUMN finding.blocked IS
    'the clauses whose citations blocked a withheld finding, with our word for why; never the model''s quote (ADR-0003)';
COMMENT ON COLUMN finding.derived_from IS
    'the finding a derived finding comes from through an approved policy mapping (F6, #306)';

COMMIT;
