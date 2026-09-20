-- Ledger schema v1 (PHASES.md Phase 0).
--
-- The ledger is the one non-derivable store (D-09). Every table carries
-- tenant_id, even while multi-tenancy is deferred (D-10) — retrofitting it
-- later would touch every table and index.
--
-- assessment records corpus_version, mapping_version and as_of, so an issued
-- report can be reproduced against the law as it stood (D-05).

BEGIN;

CREATE TABLE IF NOT EXISTS tenant (
    tenant_id     TEXT PRIMARY KEY,
    name          TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_user (
    tenant_id     TEXT        NOT NULL REFERENCES tenant (tenant_id),
    user_id       TEXT        NOT NULL,
    email         TEXT        NOT NULL,
    display_name  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id),
    UNIQUE (tenant_id, email)
);

-- --------------------------------------------------------------------------
-- Corpus. Clause text itself lives in Cloud Storage; these rows are the index.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS instrument (
    tenant_id     TEXT        NOT NULL REFERENCES tenant (tenant_id),
    instrument_id TEXT        NOT NULL,
    title         TEXT        NOT NULL,
    jurisdiction  TEXT        NOT NULL,
    scope         TEXT        NOT NULL DEFAULT 'public'
                  CHECK (scope IN ('public', 'tenant', 'user')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, instrument_id)
);

CREATE TABLE IF NOT EXISTS instrument_version (
    tenant_id     TEXT        NOT NULL,
    instrument_id TEXT        NOT NULL,
    version_id    TEXT        NOT NULL,
    title         TEXT        NOT NULL,
    status        TEXT        NOT NULL
                  CHECK (status IN ('in_force', 'not_yet_commenced', 'repealed')),
    valid_from    DATE        NOT NULL,
    valid_to      DATE,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    supersedes    TEXT,
    source_url    TEXT,
    PRIMARY KEY (tenant_id, instrument_id, version_id),
    FOREIGN KEY (tenant_id, instrument_id) REFERENCES instrument (tenant_id, instrument_id),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE IF NOT EXISTS clause (
    tenant_id     TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    clause_path   TEXT NOT NULL,
    heading       TEXT,
    PRIMARY KEY (tenant_id, instrument_id, clause_path),
    FOREIGN KEY (tenant_id, instrument_id) REFERENCES instrument (tenant_id, instrument_id)
);

-- One row per clause per instrument version. clause_id is the stable ID from
-- CONTEXT.md: {instrument_id}:{version_id}:{clause_path}.
CREATE TABLE IF NOT EXISTS clause_version (
    tenant_id       TEXT        NOT NULL,
    clause_id       TEXT        NOT NULL,
    instrument_id   TEXT        NOT NULL,
    version_id      TEXT        NOT NULL,
    clause_path     TEXT        NOT NULL,
    canonical_sha256 TEXT       NOT NULL,
    storage_uri     TEXT        NOT NULL,
    char_start      INTEGER,
    char_end        INTEGER,
    page            INTEGER,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, clause_id),
    FOREIGN KEY (tenant_id, instrument_id, version_id)
        REFERENCES instrument_version (tenant_id, instrument_id, version_id),
    FOREIGN KEY (tenant_id, instrument_id, clause_path)
        REFERENCES clause (tenant_id, instrument_id, clause_path)
);

CREATE INDEX IF NOT EXISTS clause_version_by_instrument
    ON clause_version (tenant_id, instrument_id, version_id);

-- --------------------------------------------------------------------------
-- Assessment. Loop B's record.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS document_manifest (
    tenant_id     TEXT        NOT NULL REFERENCES tenant (tenant_id),
    document_id   TEXT        NOT NULL,
    user_id       TEXT,
    filename      TEXT        NOT NULL,
    content_sha256 TEXT       NOT NULL,
    storage_uri   TEXT        NOT NULL,
    scope         TEXT        NOT NULL DEFAULT 'tenant'
                  CHECK (scope IN ('public', 'tenant', 'user')),
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_id)
);

CREATE TABLE IF NOT EXISTS assessment (
    tenant_id       TEXT        NOT NULL REFERENCES tenant (tenant_id),
    assessment_id   TEXT        NOT NULL,
    user_id         TEXT,
    document_id     TEXT,
    corpus_version  TEXT        NOT NULL,
    mapping_version TEXT        NOT NULL,
    as_of           DATE        NOT NULL,
    trace_id        TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, assessment_id),
    FOREIGN KEY (tenant_id, document_id) REFERENCES document_manifest (tenant_id, document_id)
);

CREATE INDEX IF NOT EXISTS assessment_by_trace ON assessment (trace_id);

-- No confidence score column, by decision (D-06): coverage and provenance only.
CREATE TABLE IF NOT EXISTS finding (
    tenant_id       TEXT        NOT NULL,
    finding_id      TEXT        NOT NULL,
    assessment_id   TEXT        NOT NULL,
    claim_text      TEXT        NOT NULL,
    obligation_id   TEXT,
    policy_clause_id TEXT,
    verdict         TEXT        NOT NULL
                    CHECK (verdict IN ('consistent', 'conflicts', 'not_addressed', 'ambiguous')),
    provenance_tier TEXT        NOT NULL
                    CHECK (provenance_tier IN ('parsed', 'asserted', 'inferred', 'external')),
    assessable      BOOLEAN     NOT NULL DEFAULT TRUE,
    unassessable_reason TEXT,
    citations       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    trace_id        TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, finding_id),
    FOREIGN KEY (tenant_id, assessment_id) REFERENCES assessment (tenant_id, assessment_id),
    CHECK (assessable OR unassessable_reason IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS finding_by_assessment ON finding (tenant_id, assessment_id);

-- Evidence: documents inform, humans assert (CONTEXT.md).
CREATE TABLE IF NOT EXISTS evidence (
    tenant_id       TEXT        NOT NULL REFERENCES tenant (tenant_id),
    evidence_id     TEXT        NOT NULL,
    condition_id    TEXT        NOT NULL,
    asserted_by     TEXT,
    document_id     TEXT,
    value           JSONB       NOT NULL,
    provenance_tier TEXT        NOT NULL
                    CHECK (provenance_tier IN ('parsed', 'asserted', 'inferred', 'external')),
    satisfied       BOOLEAN,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, evidence_id),
    FOREIGN KEY (tenant_id, document_id) REFERENCES document_manifest (tenant_id, document_id)
);

-- --------------------------------------------------------------------------
-- Chat and usage.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS chat (
    tenant_id     TEXT        NOT NULL REFERENCES tenant (tenant_id),
    chat_id       TEXT        NOT NULL,
    user_id       TEXT,
    turn_index    INTEGER     NOT NULL,
    role          TEXT        NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content       TEXT        NOT NULL,
    trace_id      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, chat_id, turn_index)
);

CREATE TABLE IF NOT EXISTS usage (
    tenant_id       TEXT        NOT NULL REFERENCES tenant (tenant_id),
    usage_id        TEXT        NOT NULL,
    trace_id        TEXT        NOT NULL,
    model           TEXT        NOT NULL,
    input_tokens    INTEGER     NOT NULL DEFAULT 0,
    output_tokens   INTEGER     NOT NULL DEFAULT 0,
    cached_prefix_hit BOOLEAN   NOT NULL DEFAULT FALSE,
    cost_usd        NUMERIC(12, 6) NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, usage_id)
);

CREATE INDEX IF NOT EXISTS usage_by_trace ON usage (trace_id);

COMMIT;
