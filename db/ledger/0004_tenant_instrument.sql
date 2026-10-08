-- Tenant subscriptions to library law (CONTEXT.md: Library, Subscription; ADR-0020).
--
-- One row per instrument a tenant has ticked. Retrieval is restricted to the
-- tenant's own documents plus the library instruments it has a row for here.
-- Unsubscribe deletes the row and nothing else: library instruments are shared,
-- so one tenant unticking must never touch any store another tenant reads.
-- Resubscribing inserts a fresh row; nothing is re-embedded.
--
-- `_public` has no rows. It is not a row in `tenant` at all (0002's CHECK), and
-- it owns every library chunk, so retrieval as `_public` sees the whole library
-- by ownership rather than by subscription — including instruments added later.
--
-- instrument_id is validated as an unprefixed (library) id rather than a
-- foreign key to `instrument`, because library law can't currently be inserted
-- into `instrument` at all (issue #67). Once that's fixed this becomes a real FK.

BEGIN;

CREATE TABLE IF NOT EXISTS tenant_instrument (
    tenant_id     TEXT        NOT NULL REFERENCES tenant (tenant_id),
    instrument_id TEXT        NOT NULL,
    subscribed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    subscribed_by TEXT        NOT NULL,
    PRIMARY KEY (tenant_id, instrument_id),
    -- A tenant subscribes to library law only. Its own documents are already
    -- in its environment; another tenant's are never reachable.
    CONSTRAINT tenant_instrument_is_library CHECK (
        instrument_id <> ''
        AND instrument_id NOT LIKE '%/%'
        AND instrument_id NOT LIKE '%:%'
    )
);

COMMIT;
