-- Tenant id validation at the database boundary (issue #3).
--
-- Mirrors validate_tenant_id() in compliance_rag.corpus.models: neither ':'
-- nor '/' (both are stable-ID delimiters, CONTEXT.md: Tenant id), not empty,
-- and '_public' is reserved for public law and may never be a real tenant.
--
-- The application enforces this at tenant creation (Profile.__post_init__);
-- this constraint is the second line of defence for any row that reaches
-- Postgres by a path the application layer didn't validate.

BEGIN;

ALTER TABLE tenant
    ADD CONSTRAINT tenant_id_is_valid CHECK (
        tenant_id <> ''
        AND tenant_id NOT LIKE '%:%'
        AND tenant_id NOT LIKE '%/%'
        AND tenant_id <> '_public'
    );

COMMIT;
