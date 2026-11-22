-- The list of checks (#355): a tenant's checks, newest first, paged by a
-- (created_at, check_id) cursor, for an admin (the tenant's) and for a member
-- (their own, by the submitter's uid, ADR-0059 Q5).
--
-- `LedgerStore.list_checks` reads `submitted_check` ordered by
-- `created_at DESC, check_id COLLATE "C" DESC`, filtered by tenant and, for a
-- member, by `user_id`. The primary key (tenant_id, check_id) serves neither
-- order, so each read would sort the tenant's every check. These two indexes
-- are that order, for the two filters, and the sidebar's five most recent
-- checks (every page) reads the first five entries of one of them.
--
-- Idempotent: both statements are guarded, so the runner (#293) and psql -f
-- both re-run it safely. Indexes only: no row, column or constraint changes.

BEGIN;

CREATE INDEX IF NOT EXISTS submitted_check_by_tenant_recent
    ON submitted_check (tenant_id, created_at DESC, check_id COLLATE "C" DESC);

CREATE INDEX IF NOT EXISTS submitted_check_by_submitter_recent
    ON submitted_check (tenant_id, user_id, created_at DESC, check_id COLLATE "C" DESC);

COMMIT;
