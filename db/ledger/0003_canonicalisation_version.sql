-- Canonicalisation version (issue #2).
--
-- CONTEXT.md: Canonicalisation version — every citation records the version
-- it was verified under, so changing canonicalise() can never silently
-- invalidate citations already issued.
--
-- clause_version.canonical_sha256 already pins the bytes; this column pins
-- the transformation that produced them. finding.citations is a JSONB array
-- (one element per compliance_rag.corpus.models.Citation) — the CHECK below
-- is the second line of defence for that same field on the payload, mirroring
-- the application-level Citation.canonicalisation_version field.

BEGIN;

ALTER TABLE clause_version
    ADD COLUMN canonicalisation_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE finding
    ADD CONSTRAINT finding_citations_carry_canonicalisation_version CHECK (
        NOT jsonb_path_exists(
            citations,
            '$[*] ? (!exists(@.canonicalisation_version) '
            '|| @.canonicalisation_version.type() != "number" '
            '|| @.canonicalisation_version <= 0)'
        )
    );

COMMIT;
