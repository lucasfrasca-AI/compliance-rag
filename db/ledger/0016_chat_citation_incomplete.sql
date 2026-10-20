-- A chat citation records the incompleteness the member was shown (#228,
-- ADR-0047).
--
-- No structural change: `citations` is JSONB and 0014's chat_turn_shape looks
-- at the array and its length, not its elements, so an element gaining an
-- `incomplete` key needs no DDL. What went stale is 0014's column comment,
-- which describes the element as {clause_id, version_id}; 0014 is applied and
-- stays as it is, so this re-sets the comment. Idempotent and append-only.

BEGIN;

COMMENT ON COLUMN chat.citations IS
    'a verified answer''s citations: [{clause_id, version_id, incomplete?}], never the model''s quote; '
    'incomplete is the clause''s reasons as the member was shown them (ADR-0047), present only when there are any, absent means complete (#228)';

COMMIT;
