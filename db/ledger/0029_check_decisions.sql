-- What members decided about a check's claims and findings (#301, ADR-0053 F5,
-- ADR-0059 Q3 and C1).
--
-- A claim is `inferred` and is never updated (0025); a finding is `inferred` and is
-- never updated (0026, ADR-0034: an assessment keeps what it said). A member's
-- decision sits **beside** them, in this one append-only table:
--
-- * `claim_confirmed`: the claim says what the document means. Lifts the claim to
--   `asserted` in its findings' tier. It is not evidence: nothing is written to
--   `evidence` and no coverage changes (CONTEXT.md: Claim confirmation).
-- * `claim_rejected`: it does not. The claim's findings leave the report, which says
--   who rejected it. The claim row is untouched.
-- * `verdict_accepted` / `verdict_withdrawn`: a person accepts a finding's verdict,
--   which lifts the verdict to `asserted`; or takes the acceptance back.
--
-- **Latest wins, and nothing is ever UPDATEd or DELETEd.** A confirmation may be
-- followed by a rejection and the reverse; an acceptance by its withdrawal and the
-- reverse. The current state of a claim or a verdict is its latest row
-- (`decision_id` is the order they were made in, an identity, never a clock), and the
-- history stays as the record of who decided what.
--
-- A finding's tier is computed when it is read, never stored: the weakest of its
-- claim's tier (`asserted` iff the latest claim decision is a confirmation) and its
-- verdict's (`asserted` iff the latest verdict decision is an acceptance). Only a
-- finding with a verdict can be accepted: the store refuses a withheld, failed or
-- conditional one (the finding's `outcome` is read in the same statement that
-- inserts), because the database can't say "this foreign key's row has outcome X".
--
-- Who: the stable sign-in uid (ADR-0036) and the address shown beside the decision.
-- Each row carries the request's `trace_id`.
--
-- Idempotent: every statement is guarded, so the runner (#293) and psql -f both
-- re-run it safely.

BEGIN;

CREATE TABLE IF NOT EXISTS check_decision (
    decision_id BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   TEXT        NOT NULL,
    check_id    TEXT        NOT NULL,
    kind        TEXT        NOT NULL,
    claim_index INTEGER,
    finding_id  TEXT,
    user_id     TEXT        NOT NULL,
    email       TEXT        NOT NULL,
    trace_id    TEXT        NOT NULL,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, check_id) REFERENCES check_extraction (tenant_id, check_id),
    FOREIGN KEY (tenant_id, check_id, claim_index)
        REFERENCES check_claim (tenant_id, check_id, claim_index),
    FOREIGN KEY (tenant_id, finding_id) REFERENCES finding (tenant_id, finding_id),
    CONSTRAINT check_decision_kind CHECK (
        kind IN ('claim_confirmed', 'claim_rejected', 'verdict_accepted', 'verdict_withdrawn')),
    -- a claim decision names its claim and no finding; a verdict decision its finding
    -- and no claim
    CONSTRAINT check_decision_subject CHECK (
        (kind IN ('claim_confirmed', 'claim_rejected')
            AND claim_index IS NOT NULL AND finding_id IS NULL)
        OR (kind IN ('verdict_accepted', 'verdict_withdrawn')
            AND finding_id IS NOT NULL AND claim_index IS NULL)),
    CONSTRAINT check_decision_user_id CHECK (btrim(user_id) <> ''),
    CONSTRAINT check_decision_email CHECK (btrim(email) <> ''),
    CONSTRAINT check_decision_trace_id CHECK (btrim(trace_id) <> '')
);

-- A check's decisions in the order they were made: the one read of a report, and the
-- "latest of this claim" the insert's guard looks at.
CREATE INDEX IF NOT EXISTS check_decision_by_check
    ON check_decision (tenant_id, check_id, decision_id);

COMMENT ON TABLE check_decision IS
    'a member''s claim confirmation, claim rejection, verdict acceptance or its withdrawal; append-only, latest wins; never changes a claim or a finding (#301, ADR-0059 Q3)';
COMMENT ON COLUMN check_decision.user_id IS
    'who decided, by the stable sign-in uid (ADR-0036)';
COMMENT ON COLUMN check_decision.email IS
    'the address shown beside the decision ("rejected by ..."), as it was when it was made';
COMMENT ON COLUMN check_decision.decision_id IS
    'the order decisions were made in; the latest of a claim or a finding is its current state';
COMMENT ON COLUMN check_decision.trace_id IS
    'the trace_id of the request that made the decision';

COMMIT;
