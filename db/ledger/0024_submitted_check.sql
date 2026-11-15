-- A check: one member's submission of a document to be checked (#297, ADR-0059).
--
-- `assessment` does not fit it. An assessment records the corpus and mapping
-- commits it was computed against (0013 makes both mandatory on every insert)
-- and, by its column comment, the asker's email, and a check has neither at the
-- moment it is submitted: no model has run, so there is no corpus or mapping
-- version to name, and a member's uid (ADR-0036) is what owns it. So this is a
-- table of its own. The assessment #299 records points back at it by
-- `check_id`, and the check points at the `submitted_document` version it
-- judges (ADR-0059 Q1), which is an instrument version like any other
-- (ADR-0021: versioned, withdrawn and purged as any document is).
--
-- Append-only: a check is written once, by `POST /v1/checks`, after the
-- document's version is recorded, and nothing updates or deletes a row.
--
-- Visibility is not in this table (ADR-0059 Q5): a check is its submitter's, by
-- the sign-in's uid, and the tenant's admins', who are configuration
-- (`API_ADMINS`), so the API decides it from `user_id` and the configured list.
-- The database keeps the one fact the decision needs, who submitted it.
--
-- One check per submitted version: a version is one document, and a second
-- submission is a new version of its own (ADR-0034: an assessment keeps the
-- document it judged).

BEGIN;

CREATE TABLE IF NOT EXISTS submitted_check (
    tenant_id       TEXT        NOT NULL REFERENCES tenant (tenant_id),
    check_id        TEXT        NOT NULL,
    user_id         TEXT        NOT NULL,
    instrument_id   TEXT        NOT NULL,
    version_id      TEXT        NOT NULL,
    filename        TEXT        NOT NULL,
    document_format TEXT        NOT NULL,
    trace_id        TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, check_id),
    FOREIGN KEY (tenant_id, instrument_id, version_id)
        REFERENCES instrument_version (tenant_id, instrument_id, version_id),
    UNIQUE (tenant_id, instrument_id, version_id),
    CONSTRAINT submitted_check_user_id CHECK (btrim(user_id) <> ''),
    CONSTRAINT submitted_check_trace_id CHECK (btrim(trace_id) <> ''),
    CONSTRAINT submitted_check_filename CHECK (btrim(filename) <> ''),
    CONSTRAINT submitted_check_format CHECK (document_format IN ('markdown', 'text'))
);

COMMENT ON TABLE submitted_check IS
    'one member''s submission of a document to be checked (#297, ADR-0059): the check, pointing at its submitted_document version';
COMMENT ON COLUMN submitted_check.user_id IS
    'the submitter''s uid (ADR-0036), never an email; the check is theirs and the tenant admins'' alone (ADR-0059 Q5)';
COMMENT ON COLUMN submitted_check.trace_id IS
    'the trace_id of the request that submitted it';

COMMIT;
