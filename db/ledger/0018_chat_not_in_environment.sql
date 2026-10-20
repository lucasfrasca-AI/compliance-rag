-- An answer turn records each instrument its question named that the
-- tenant's environment didn't hold (#252, ADR-0051): the flag the member was
-- shown beside the answer, so a chat read back shows it too, as #228 keeps a
-- citation's incompleteness.
--
-- [{named, known_as, part_of}], set by the pipeline from the question, never
-- by the model. Empty on a question and on a failed request (a marker only,
-- 0014); a verified or a withheld answer may carry it. Every existing row
-- takes the default, `[]`, which is what those answers were shown with.
-- Idempotent and append-only.

BEGIN;

ALTER TABLE chat ADD COLUMN IF NOT EXISTS not_in_environment JSONB NOT NULL DEFAULT '[]'::jsonb;

-- A CASE, as 0014's chat_turn_shape: jsonb_array_length raises on a scalar, so
-- the array check must come first, and only a CASE promises the order.
ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_not_in_environment;
ALTER TABLE chat ADD CONSTRAINT chat_not_in_environment CHECK (
    CASE
        WHEN jsonb_typeof(not_in_environment) IS DISTINCT FROM 'array'
            THEN FALSE
        WHEN role = 'user' OR outcome IN ('residency', 'malformed_generation', 'internal')
            THEN jsonb_array_length(not_in_environment) = 0
        ELSE TRUE
    END
);

COMMENT ON COLUMN chat.not_in_environment IS
    'an answer''s flags: [{named, known_as, part_of}], each instrument its question named that '
    'the tenant''s environment didn''t hold, set by the pipeline from the question, never the model '
    '(#252, ADR-0051); empty on a question and a failed request';

COMMIT;
