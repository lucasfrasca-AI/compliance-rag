-- A fourth document class, `submitted_document` (#296, ADR-0053 U1, ADR-0059).
--
-- A document a member submits to be checked is stored as a versioned document
-- of its own class, so ADR-0021's versioning, withdraw and purge are reused. It
-- is never chunked, never retrieved and never in the tenant's environment
-- (`policy_versions` lists internal policies only; `environment_titles`
-- leaves this class out). The class belongs to the instrument, as the other
-- three do, and a tenant's alone: `instrument_library_law_is_public` (0011)
-- still keeps `library_law` to `_public` and nothing else.
--
-- Idempotent and append-only: 0011's inline check, which Postgres named
-- `instrument_document_class_check`, is replaced by the same check with one
-- more admitted value. No row is touched.

BEGIN;

ALTER TABLE instrument DROP CONSTRAINT IF EXISTS instrument_document_class_check;
ALTER TABLE instrument
    ADD CONSTRAINT instrument_document_class_check CHECK (
        document_class IN (
            'library_law', 'external_upload', 'internal_policy', 'submitted_document'
        )
    );

COMMENT ON COLUMN instrument.document_class IS
    'library_law, external_upload, internal_policy or submitted_document (#148, #296): fixed per instrument; only internal_policy is the cached prefix, and submitted_document is the subject of a check, never retrieved and never in the environment (ADR-0053 U1)';

COMMIT;
