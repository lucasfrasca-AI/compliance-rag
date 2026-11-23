-- An admin approves or rejects a proposed policy mapping, and an approved one derives
-- findings (#306, ADR-0053 F6, ADR-0059 Q7 and C1, ADR-0027 point 7).
--
-- **A decision is an UPDATE of exactly one row, once, and nothing else of the row changes.**
-- 0031 gave `policy_mapping` the `status` and `approver` columns for this, under a CHECK
-- (a proposal has no approver, a decided mapping has one). The schema could not carry the
-- decision's own request, though: `trace_id` is the *proposing* request's, and the ticket
-- wants the approver and the `trace_id` of the approval, and of a rejection. Rather than a
-- second table beside the row (two writes that could disagree), the row takes the
-- decision's columns: `approver_email` (the address shown; `approver` is the stable sign-in
-- uid, ADR-0036), `decided_at`, `decision_trace_id`, and a rejection's optional
-- `rejection_reason`. They are set together with `status` or not at all
-- (`policy_mapping_decided`), so a mapping keeps both traces: who proposed it in which
-- request, and who decided it in which.
--
-- The guard is not the application's alone. `policy_mapping_guard` refuses, whatever the
-- caller: a DELETE; any UPDATE of a mapping that is not `proposed` (a decided mapping never
-- changes, even to the same status); an UPDATE that leaves it `proposed`; and an UPDATE
-- that changes any column but the decision's (the policy clause, the obligation, the kind,
-- the rationale, the proposer, the proposing `trace_id` and the rest are as the model's
-- run wrote them). The store's statement is `UPDATE ... WHERE status = 'proposed'`, so of
-- two admins deciding at once one decides it and the other finds nothing to update.
--
-- **A derived finding** (F6) is a `finding` row made by code in a findings run: the claim's
-- finding against a policy clause, carried over to the obligation that an approved
-- `implements` mapping says the clause meets. It points at its source (`derived_from`, 0026)
-- and at the mapping (`mapping_id`, here), always both or neither, and is always a verdict
-- against an obligation. **The model's own finding for the same claim and obligation stays**
-- (ADR-0059 Q7: neither is dropped), so the one-finding-per-claim-and-target key now
-- includes `derived_from`: one of the model's own, and one derived per source finding.
-- The mapping is named with the tenant, so a finding cannot point at another tenant's.
--
-- Idempotent: every statement is guarded, so the runner (#293) and psql -f both re-run it
-- safely.

BEGIN;

ALTER TABLE policy_mapping ADD COLUMN IF NOT EXISTS approver_email TEXT;
ALTER TABLE policy_mapping ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;
ALTER TABLE policy_mapping ADD COLUMN IF NOT EXISTS decision_trace_id TEXT;
ALTER TABLE policy_mapping ADD COLUMN IF NOT EXISTS rejection_reason TEXT;

-- who, when and in which request: all set with a decision, none without one
ALTER TABLE policy_mapping DROP CONSTRAINT IF EXISTS policy_mapping_decided;
ALTER TABLE policy_mapping ADD CONSTRAINT policy_mapping_decided CHECK (
    (status = 'proposed'
        AND approver_email IS NULL AND decided_at IS NULL AND decision_trace_id IS NULL)
    OR (status <> 'proposed'
        AND approver_email IS NOT NULL AND btrim(approver_email) <> ''
        AND decided_at IS NOT NULL
        AND decision_trace_id IS NOT NULL AND btrim(decision_trace_id) <> ''));

-- a reason is a rejection's, one non-blank line: the admin's own words, never evidence
ALTER TABLE policy_mapping DROP CONSTRAINT IF EXISTS policy_mapping_reason;
ALTER TABLE policy_mapping ADD CONSTRAINT policy_mapping_reason CHECK (
    rejection_reason IS NULL
    OR (status = 'rejected'
        AND btrim(rejection_reason) <> '' AND char_length(rejection_reason) <= 300
        AND rejection_reason !~ E'[\\n\\r]'));

-- what a finding's mapping foreign key names: the mapping and its tenant (the key is dropped
-- first so that a second run can replace the constraint it depends on)
ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_mapping_fk;
ALTER TABLE policy_mapping DROP CONSTRAINT IF EXISTS policy_mapping_tenant_mapping;
ALTER TABLE policy_mapping ADD CONSTRAINT policy_mapping_tenant_mapping
    UNIQUE (tenant_id, mapping_id);

CREATE OR REPLACE FUNCTION policy_mapping_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a policy mapping is never deleted'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'policy_mapping_guard';
    END IF;
    IF OLD.status <> 'proposed' THEN
        RAISE EXCEPTION 'a decided policy mapping never changes'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'policy_mapping_guard';
    END IF;
    IF NEW.status = 'proposed' THEN
        RAISE EXCEPTION 'a policy mapping is only updated to decide it'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'policy_mapping_guard';
    END IF;
    IF ROW(NEW.mapping_id, NEW.tenant_id, NEW.policy_instrument_id, NEW.policy_version_id,
           NEW.policy_clause_id, NEW.obligation_id, NEW.kind, NEW.rationale, NEW.proposer,
           NEW.trace_id, NEW.proposed_at)
       IS DISTINCT FROM
       ROW(OLD.mapping_id, OLD.tenant_id, OLD.policy_instrument_id, OLD.policy_version_id,
           OLD.policy_clause_id, OLD.obligation_id, OLD.kind, OLD.rationale, OLD.proposer,
           OLD.trace_id, OLD.proposed_at) THEN
        RAISE EXCEPTION 'a decision changes the status and the decision''s columns, nothing else'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'policy_mapping_guard';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS policy_mapping_guard ON policy_mapping;
CREATE TRIGGER policy_mapping_guard
    BEFORE UPDATE OR DELETE ON policy_mapping
    FOR EACH ROW EXECUTE FUNCTION policy_mapping_guard();

-- a derived finding: its mapping, with the source finding 0026 already names
ALTER TABLE finding ADD COLUMN IF NOT EXISTS mapping_id BIGINT;

ALTER TABLE finding ADD CONSTRAINT finding_mapping_fk
    FOREIGN KEY (tenant_id, mapping_id) REFERENCES policy_mapping (tenant_id, mapping_id);

ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_derived_together;
ALTER TABLE finding ADD CONSTRAINT finding_derived_together
    CHECK ((derived_from IS NULL) = (mapping_id IS NULL));

-- a derived finding is a verdict against an obligation (F6): a policy clause's finding is
-- what it is derived from, and a finding with no verdict has nothing to carry over
ALTER TABLE finding DROP CONSTRAINT IF EXISTS finding_derived_is_a_verdict;
ALTER TABLE finding ADD CONSTRAINT finding_derived_is_a_verdict
    CHECK (derived_from IS NULL
           OR (check_id IS NOT NULL AND outcome = 'verdict' AND obligation_id IS NOT NULL));

-- the model's own finding for a claim and target stays beside the one derived from a
-- mapping (ADR-0059 Q7): one of each per claim and target, one derived per source finding
DROP INDEX IF EXISTS finding_one_per_claim_and_target;
CREATE UNIQUE INDEX IF NOT EXISTS finding_one_per_claim_target_and_source
    ON finding (tenant_id, assessment_id, claim_index,
                COALESCE(obligation_id, ''), COALESCE(policy_clause_id, ''),
                COALESCE(derived_from, ''))
    WHERE check_id IS NOT NULL;

COMMENT ON COLUMN policy_mapping.approver IS
    'who decided it, by the stable sign-in uid (ADR-0036, #306): null while proposed';
COMMENT ON COLUMN policy_mapping.approver_email IS
    'the address shown beside the decision, as it was when it was made (#306)';
COMMENT ON COLUMN policy_mapping.decided_at IS
    'when it was approved or rejected (#306)';
COMMENT ON COLUMN policy_mapping.decision_trace_id IS
    'the trace_id of the request that approved or rejected it; trace_id stays the proposing request''s (#306)';
COMMENT ON COLUMN policy_mapping.rejection_reason IS
    'a rejection''s optional one line: the admin''s own words, never evidence (#306)';
COMMENT ON COLUMN finding.mapping_id IS
    'the approved implements mapping a derived finding went through, set with derived_from (F6, #306)';

COMMIT;
