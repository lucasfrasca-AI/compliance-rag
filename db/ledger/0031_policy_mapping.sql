-- A tenant's policy-to-law mappings, and the runs that propose them (#305, ADR-0053 F6,
-- ADR-0059 Q8 and C2, ADR-0027 point 7).
--
-- A policy mapping is a tenant's statement that one of its policy clauses meets an
-- obligation: `implements` or `partially_implements` (CONTEXT.md: Policy mapping). The
-- model proposes it; an admin approves or rejects it (#306). It is **tenant content, so
-- it is a ledger row, never repo YAML and never a graph node**: the repository is public
-- and the persistent graph holds nothing a tenant authored (ADR-0018, D-09, ADR-0059 C2).
-- This table is where a mapping lives, from `proposed` on.
--
-- **`status` starts `proposed`** and this ticket writes no other value: the column has no
-- way to be set on insert but its default, and a proposal made by the model carries no
-- approver. `approved` and `rejected` are the admin's, and #306 builds the actions;
-- the table already refuses an approval that names nobody, and a proposal that names
-- someone who has not decided.
--
-- **The proposer is the model id** (`gemini-3.5-flash`), never a person. **The rationale
-- is the model's own words**: one line, at most 300 characters, shown only labelled as
-- the model's and never evidence. It is never put in a prompt.
--
-- **Idempotent proposing.** The same (policy clause, obligation, kind) proposed twice is
-- one row: the unique key below, which `INSERT ... ON CONFLICT DO NOTHING` keeps. A
-- rejected row is therefore never proposed again by a later run, and an approved one is
-- never duplicated. (The clause id carries the policy version, so the key is per version.)
-- A new policy version has new clause ids, and so needs its own approvals (Q8).
--
-- `policy_mapping_run` is the reservation, as `check_extraction` is for a check: one row
-- per policy version, `pending` while a run holds it, so of any number of concurrent
-- requests exactly one proposes and the rest never call the model. Unlike a check's
-- extraction a version may be proposed for again (a `done` row can be reserved), since the
-- unique key above keeps a repeat from adding a row. A `failed` row, or a `pending` one
-- older than the store's time-out (a request that died holding it), may be reserved again.
--
-- Every row carries the `trace_id` of the request that made it.
--
-- Idempotent: every statement is guarded, so the runner (#293) and psql -f both re-run it
-- safely.

BEGIN;

CREATE TABLE IF NOT EXISTS policy_mapping_run (
    tenant_id           TEXT        NOT NULL,
    policy_instrument_id TEXT       NOT NULL,
    policy_version_id   TEXT        NOT NULL,
    trace_id            TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'pending',
    reserved_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, policy_instrument_id, policy_version_id),
    FOREIGN KEY (tenant_id, policy_instrument_id, policy_version_id)
        REFERENCES instrument_version (tenant_id, instrument_id, version_id),
    CONSTRAINT policy_mapping_run_trace_id CHECK (btrim(trace_id) <> ''),
    CONSTRAINT policy_mapping_run_status CHECK (status IN ('pending', 'done', 'failed')),
    CONSTRAINT policy_mapping_run_completed CHECK ((status = 'done') = (completed_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS policy_mapping (
    mapping_id           BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id            TEXT        NOT NULL,
    policy_instrument_id TEXT        NOT NULL,
    policy_version_id    TEXT        NOT NULL,
    policy_clause_id     TEXT        NOT NULL,
    obligation_id        TEXT        NOT NULL,
    kind                 TEXT        NOT NULL,
    status               TEXT        NOT NULL DEFAULT 'proposed',
    rationale            TEXT        NOT NULL,
    proposer             TEXT        NOT NULL,
    approver             TEXT,
    trace_id             TEXT        NOT NULL,
    proposed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, policy_instrument_id, policy_version_id)
        REFERENCES instrument_version (tenant_id, instrument_id, version_id),
    CONSTRAINT policy_mapping_once UNIQUE (tenant_id, policy_clause_id, obligation_id, kind),
    CONSTRAINT policy_mapping_kind CHECK (kind IN ('implements', 'partially_implements')),
    CONSTRAINT policy_mapping_status CHECK (status IN ('proposed', 'approved', 'rejected')),
    -- the clause is one of the version's: its stable id begins with the version's own
    CONSTRAINT policy_mapping_clause_of_version CHECK (
        starts_with(policy_clause_id,
                    policy_instrument_id || ':' || policy_version_id || ':')
        AND length(policy_clause_id) >
            length(policy_instrument_id || ':' || policy_version_id || ':')),
    CONSTRAINT policy_mapping_obligation_id CHECK (
        obligation_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$'),
    CONSTRAINT policy_mapping_rationale CHECK (
        btrim(rationale) <> '' AND char_length(rationale) <= 300 AND rationale !~ E'[\\n\\r]'),
    CONSTRAINT policy_mapping_proposer CHECK (btrim(proposer) <> ''),
    CONSTRAINT policy_mapping_trace_id CHECK (btrim(trace_id) <> ''),
    -- a proposal has nobody who decided it; an approval has somebody
    CONSTRAINT policy_mapping_approver CHECK (
        (status = 'proposed' AND approver IS NULL)
        OR (status <> 'proposed' AND approver IS NOT NULL AND btrim(approver) <> ''))
);

-- A policy version's mappings in the order they were proposed: the page's list, and
-- what #306's derivation reads.
CREATE INDEX IF NOT EXISTS policy_mapping_by_version
    ON policy_mapping (tenant_id, policy_instrument_id, policy_version_id, mapping_id);

COMMENT ON TABLE policy_mapping_run IS
    'the one proposing run of a policy version at a time (#305, ADR-0059 Q8); reserved before the model is called';
COMMENT ON TABLE policy_mapping IS
    'a tenant''s policy-to-law mapping: proposed by the model, approved or rejected by an admin (#306); a ledger row, never repo YAML or a graph node (#305, ADR-0059 C2, ADR-0018)';
COMMENT ON COLUMN policy_mapping.kind IS
    'implements or partially_implements; only implements derives a finding (ADR-0053 F6)';
COMMENT ON COLUMN policy_mapping.proposer IS
    'the model that proposed it, by id; never a person';
COMMENT ON COLUMN policy_mapping.approver IS
    'who decided it (#306): null while proposed';
COMMENT ON COLUMN policy_mapping.rationale IS
    'the model''s one-line reason: its words, shown labelled as the model''s, never evidence and never put in a prompt';
COMMENT ON COLUMN policy_mapping.trace_id IS
    'the trace_id of the request that proposed it';

COMMIT;
