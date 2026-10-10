-- A tenant's profile (issue #69): the structured facts `LedgerStore.put_profile`
-- writes (CONTEXT.md: Profile). One JSONB document per tenant, on the tenant
-- row — never embedded (D-14). NULL until a profile is recorded.

BEGIN;

ALTER TABLE tenant ADD COLUMN IF NOT EXISTS profile JSONB;

COMMIT;

-- The removal audit's insertion order (issue #69): `recorded_at` is the
-- transaction start and can tie, and `audit_id` is not ordered, so a sequence
-- carries the order the trail must be read back in.
BEGIN;

ALTER TABLE corpus_removal_audit ADD COLUMN IF NOT EXISTS seq BIGSERIAL;

COMMIT;
