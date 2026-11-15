-- The claims extracted from a check's document (#298, ADR-0053 F2, ADR-0059 Q2, Q4).
--
-- A claim is a verbatim span of one passage of the submitted document, plus the
-- model's one-line label and the obligations and policy clauses the model picked
-- to check it against. It is `inferred`: extracted from prose, never promoted or
-- mutated (CONTEXT.md: Claim). Findings (#299) and claim confirmation (#301) are
-- later tables that point at a claim by (tenant_id, check_id, claim_index).
--
-- **The ledger does not carry the document's words** (as `submitted_check` does
-- not). A claim names its span by passage and character offsets in the document,
-- with the sha256 of the span's text, and is read back from the stored passage:
-- a document purged (ADR-0021) takes its quotes with it. The summary is the
-- model's label and is the one piece of text here.
--
-- Two tables. `check_extraction` is one row per check, so a check is extracted
-- once whether or not it yielded a claim (a repeated request does not spend the
-- model again) and it records how many claims the model returned that failed a
-- check; `check_claim` is one row per accepted claim, 25 at most. Both carry the
-- request's trace_id. Append-only: nothing updates or deletes a row.
--
-- Idempotent: every statement is guarded, so the runner (#293) and psql -f both
-- re-run it safely.

BEGIN;

CREATE TABLE IF NOT EXISTS check_extraction (
    tenant_id      TEXT        NOT NULL,
    check_id       TEXT        NOT NULL,
    as_of          DATE        NOT NULL,
    trace_id       TEXT        NOT NULL,
    claims_dropped INTEGER     NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, check_id),
    FOREIGN KEY (tenant_id, check_id) REFERENCES submitted_check (tenant_id, check_id),
    CONSTRAINT check_extraction_trace_id CHECK (btrim(trace_id) <> ''),
    CONSTRAINT check_extraction_dropped CHECK (claims_dropped >= 0)
);

CREATE TABLE IF NOT EXISTS check_claim (
    tenant_id         TEXT    NOT NULL,
    check_id          TEXT    NOT NULL,
    claim_index       INTEGER NOT NULL,
    passage           TEXT    NOT NULL,
    quote_start       INTEGER NOT NULL,
    quote_end         INTEGER NOT NULL,
    quote_sha256      TEXT    NOT NULL,
    summary           TEXT    NOT NULL,
    obligation_ids    TEXT[]  NOT NULL,
    policy_clause_ids TEXT[]  NOT NULL,
    provenance        TEXT    NOT NULL,
    trace_id          TEXT    NOT NULL,
    PRIMARY KEY (tenant_id, check_id, claim_index),
    FOREIGN KEY (tenant_id, check_id) REFERENCES check_extraction (tenant_id, check_id),
    CONSTRAINT check_claim_index CHECK (claim_index BETWEEN 1 AND 25),
    CONSTRAINT check_claim_passage CHECK (passage ~ '^p[1-9][0-9]*$'),
    CONSTRAINT check_claim_span CHECK (quote_start >= 0 AND quote_end > quote_start),
    CONSTRAINT check_claim_sha256 CHECK (quote_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT check_claim_summary CHECK (btrim(summary) <> '' AND summary !~ E'[\\n\\r]'),
    CONSTRAINT check_claim_provenance CHECK (provenance = 'inferred'),
    CONSTRAINT check_claim_trace_id CHECK (btrim(trace_id) <> '')
);

COMMENT ON TABLE check_extraction IS
    'the claims extracted from one check''s document, once per check (#298, ADR-0059 Q4)';
COMMENT ON TABLE check_claim IS
    'one claim of a submitted document: a span of a passage, by offsets and the sha256 of its text, never the text itself; inferred (#298, ADR-0059 Q2)';
COMMENT ON COLUMN check_claim.quote_start IS
    'character offset in the document (code points), inside the passage''s own char_start:char_end';
COMMENT ON COLUMN check_claim.summary IS
    'the model''s one-line label: it only labels the quote, and is never evidence';

COMMIT;
