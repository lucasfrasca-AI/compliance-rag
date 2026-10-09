-- The library owner (issue #67, ADR-0025).
--
-- Library law is owned by `_public` (ADR-0020), but every corpus table chains
-- to `tenant`, and 0002 forbade `_public` there — so no public-law instrument
-- or version could be recorded at all. The fix keeps both guarantees:
--
-- * `_public` exists as exactly one row, flagged `is_library`, seeded here.
--   The CHECK ties the flag to that id both ways: no other row may be a
--   library, and `_public` may not be anything else.
-- * No company can ever register as `_public`: the seeded row holds the
--   primary key, so any later insert of `_public` collides with it.
--
-- `tenant_instrument` gains a real foreign key to the library instrument it
-- names, through a generated column fixed at '_public' — a subscription can
-- only point at an instrument the library actually holds.

BEGIN;

ALTER TABLE tenant ADD COLUMN IF NOT EXISTS is_library BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE tenant DROP CONSTRAINT IF EXISTS tenant_id_is_valid;
ALTER TABLE tenant
    ADD CONSTRAINT tenant_id_is_valid CHECK (
        tenant_id <> ''
        AND tenant_id NOT LIKE '%:%'
        AND tenant_id NOT LIKE '%/%'
        AND (tenant_id = '_public') = is_library
    );

INSERT INTO tenant (tenant_id, name, is_library)
VALUES ('_public', 'Library (public law)', TRUE)
ON CONFLICT (tenant_id) DO NOTHING;

-- `_public` is now a tenant row, so the FK to `tenant` no longer keeps it out
-- of `tenant_instrument` on its own. It sees the library by ownership and
-- never subscribes (ADR-0020); say so directly.
ALTER TABLE tenant_instrument
    ADD CONSTRAINT tenant_instrument_subscriber_is_not_the_library
    CHECK (tenant_id <> '_public');

ALTER TABLE tenant_instrument
    ADD COLUMN IF NOT EXISTS library_tenant_id TEXT GENERATED ALWAYS AS ('_public') STORED;
ALTER TABLE tenant_instrument
    ADD CONSTRAINT tenant_instrument_library_instrument_fk
    FOREIGN KEY (library_tenant_id, instrument_id) REFERENCES instrument (tenant_id, instrument_id);

COMMIT;
