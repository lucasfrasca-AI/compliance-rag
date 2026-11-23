-- The admin's policy-version page (#303, ADR-0053 U4, ADR-0021).
--
-- Four changes, all additive or loosening:
--
-- 1. `instrument_version.backdate_reason`: why a version was given an effective
--    date in the past. A past date is allowed only with a recorded reason
--    (ADR-0053 U4); the API refuses it without one, and this column is where the
--    reason is kept with the version it explains. NULL for a version published
--    with today's date or a future one, and for every version before this file.
--    Never blank when present.
-- 2. `corpus_removal_audit.action` admits 'publish': the admin's upload of a
--    version is audited in the same append-only table as its withdraw and
--    purge, whatever the outcome (refused, authorized, completed, failed), so
--    one table holds every admin action on a version. The table's name stays
--    (a rename would break the readers of 0005); `publish` is not a removal.
-- 3. `corpus_removal_audit.trace_id`: the trace of the request an audit row
--    came from (CLAUDE.md: a ledger row stores its trace). NULL for a row an
--    operator script wrote outside any request, and for every row before this.
-- 4. `corpus_removal_audit.instrument_id` may be NULL: an upload of a new policy that
--    is refused names no instrument and made none, and the row records what the request
--    named (nothing) rather than an id that never existed.
--
-- Idempotent: the column statements are guarded and each constraint is dropped
-- before it is added, so the runner (#293) and psql -f both re-run it safely.

BEGIN;

ALTER TABLE instrument_version ADD COLUMN IF NOT EXISTS backdate_reason TEXT;

ALTER TABLE instrument_version
    DROP CONSTRAINT IF EXISTS instrument_version_backdate_reason_nonblank;
ALTER TABLE instrument_version
    ADD CONSTRAINT instrument_version_backdate_reason_nonblank CHECK (
        backdate_reason IS NULL OR btrim(backdate_reason) <> ''
    );

COMMENT ON COLUMN instrument_version.backdate_reason IS
    'why this version was given an effective date in the past (#303, ADR-0053 U4); NULL when it was not';

ALTER TABLE corpus_removal_audit ADD COLUMN IF NOT EXISTS trace_id TEXT;

ALTER TABLE corpus_removal_audit ALTER COLUMN instrument_id DROP NOT NULL;

ALTER TABLE corpus_removal_audit DROP CONSTRAINT IF EXISTS corpus_removal_audit_action_check;
ALTER TABLE corpus_removal_audit
    ADD CONSTRAINT corpus_removal_audit_action_check CHECK (
        action IN ('publish', 'withdraw', 'purge', 'replace_in_place')
    );

COMMIT;
