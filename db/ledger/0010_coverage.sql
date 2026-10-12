-- Coverage, persisted per assessment (issue #115, D-06).
--
-- One row per assessment and obligation: applicable, not applicable or
-- unassessable — never a score (D-06) — with the clauses the decision rests
-- on and, for an unassessable one, the questions that would settle it. This
-- is what the Phase 2 exit criterion "coverage report lists every
-- unassessable obligation with its reason" reads back. Every row carries the
-- trace_id of the request that produced it.

BEGIN;

CREATE TABLE IF NOT EXISTS coverage (
    tenant_id     TEXT        NOT NULL,
    assessment_id TEXT        NOT NULL,
    obligation_id TEXT        NOT NULL,
    outcome       TEXT        NOT NULL
                  CHECK (outcome IN ('applicable', 'not_applicable', 'unassessable')),
    reason        TEXT        NOT NULL CHECK (btrim(reason) <> ''),
    citations     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    questions     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    trace_id      TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, assessment_id, obligation_id),
    FOREIGN KEY (tenant_id, assessment_id) REFERENCES assessment (tenant_id, assessment_id),
    -- A decided obligation cites the law it was decided by; an undecided one
    -- asks no question only when it has no text to cite (ADR-0028).
    CHECK (outcome = 'unassessable' OR jsonb_array_length(citations) > 0),
    CHECK (outcome <> 'unassessable' OR jsonb_array_length(questions) > 0
           OR jsonb_array_length(citations) = 0)
);

COMMIT;
