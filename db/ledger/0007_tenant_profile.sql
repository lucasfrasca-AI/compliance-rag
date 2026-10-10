-- A tenant's profile (issue #69): the structured facts `LedgerStore.put_profile`
-- writes (CONTEXT.md: Profile). One JSONB document per tenant, on the tenant
-- row — never embedded (D-14). NULL until a profile is recorded.

BEGIN;

ALTER TABLE tenant ADD COLUMN IF NOT EXISTS profile JSONB;

COMMIT;
