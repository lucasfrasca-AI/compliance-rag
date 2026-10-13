-- A subscription records the request that made it (#147; CLAUDE.md: every
-- ledger row that results from a request stores its trace_id).
--
-- Nullable: subscriptions made before the API existed (tests, scripts) came
-- from no request. Every API subscription carries one. A repeat subscribe is
-- a no-op, so a row keeps the trace of the request that created it.

BEGIN;

ALTER TABLE tenant_instrument ADD COLUMN IF NOT EXISTS trace_id TEXT;

COMMIT;
