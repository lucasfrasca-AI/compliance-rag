-- Document class (issue #148; CONTEXT.md: Document class).
--
-- Which kind of document an instrument is: library law (the admin's, owned by
-- `_public`, ADR-0020), an external upload, or an internal policy. Only
-- internal policies are answer()'s cached prefix (D-11); external uploads are
-- retrieval-only. The class belongs to the instrument, so every version
-- shares it.
--
-- Rows recorded before this have no class. `_public`'s are library law by
-- construction and are backfilled. A tenant's are left NULL: a class is a fact
-- about the document, not something to guess. Reading them as policies is
-- refused (PostgresLedgerStore.policy_versions) until the next version
-- recorded for that instrument states its class. New rows always carry one.

BEGIN;

ALTER TABLE instrument
    ADD COLUMN IF NOT EXISTS document_class TEXT
        CHECK (document_class IN ('library_law', 'external_upload', 'internal_policy'));

UPDATE instrument SET document_class = 'library_law'
    WHERE tenant_id = '_public' AND document_class IS NULL;

ALTER TABLE instrument DROP CONSTRAINT IF EXISTS instrument_library_law_is_public;
ALTER TABLE instrument
    ADD CONSTRAINT instrument_library_law_is_public CHECK (
        document_class IS NULL OR (tenant_id = '_public') = (document_class = 'library_law')
    );

COMMIT;
