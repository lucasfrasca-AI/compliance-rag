-- A version is recorded with its document's real title (#239, ADR-0049).
--
-- Until now `record_version` wrote the instrument id into both title columns,
-- so no title was recorded anywhere. The title is now supplied by whoever
-- publishes the version and is what a policy citation names the document by:
-- `instrument_version.title` is that version's, `instrument.title` the latest
-- recorded version's. The adapter refuses an empty or multi-line title
-- (`validate_title`); this keeps a blank one out of the table whatever writes
-- it. No backfill: no ledger is deployed (#174), and a row written before
-- this holds a non-blank id, so the constraints validate. Idempotent.

BEGIN;

ALTER TABLE instrument DROP CONSTRAINT IF EXISTS instrument_title_nonblank;
ALTER TABLE instrument
    ADD CONSTRAINT instrument_title_nonblank CHECK (btrim(title) <> '');

ALTER TABLE instrument_version DROP CONSTRAINT IF EXISTS instrument_version_title_nonblank;
ALTER TABLE instrument_version
    ADD CONSTRAINT instrument_version_title_nonblank CHECK (btrim(title) <> '');

COMMENT ON COLUMN instrument.title IS
    'the document''s title as its latest recorded version gives it (ADR-0049); never the instrument id by default';
COMMENT ON COLUMN instrument_version.title IS
    'the document''s title as this version was published with it: what a policy citation names the document by (ADR-0049)';

COMMIT;
