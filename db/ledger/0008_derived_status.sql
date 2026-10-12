-- Status is derived, never stored (ADR-0029).
--
-- `instrument_version.status` was written as 'in_force' on every insert and
-- never changed — a retired version kept saying 'in_force' beside the
-- `valid_to` that says otherwise: the two-representations failure ADR-0029
-- removes. What remains is valid time plus why it ended: `end_reason`, set
-- with `valid_to` and only with it. Status on a date is computed from them.

BEGIN;

ALTER TABLE instrument_version ADD COLUMN IF NOT EXISTS end_reason TEXT
    CHECK (end_reason IN ('superseded', 'repealed', 'expired'));

-- Every version retired before this migration was retired by supersession:
-- `end_version` (version-and-retire, ADR-0021) is the only path that sets
-- valid_to.
UPDATE instrument_version SET end_reason = 'superseded'
    WHERE valid_to IS NOT NULL AND end_reason IS NULL;

ALTER TABLE instrument_version DROP CONSTRAINT IF EXISTS instrument_version_end_together;
ALTER TABLE instrument_version ADD CONSTRAINT instrument_version_end_together
    CHECK ((valid_to IS NULL) = (end_reason IS NULL));

ALTER TABLE instrument_version DROP COLUMN IF EXISTS status;

COMMIT;
