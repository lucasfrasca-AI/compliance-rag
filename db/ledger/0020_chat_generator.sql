-- Which generator wrote an answer turn (#292). The API can run on Claude or on
-- the in-process demo stand-in (GENERATOR), and a chat is read back long after
-- the deployment may have changed, so a turn says for itself what wrote it: the
-- pages show a "FAKE GENERATOR" banner on a turn written by the demo whatever
-- the deployment runs now, and none on a turn a model wrote.
--
-- Nullable: every row that exists takes NULL (written before this column, so
-- unknown, and shown as nothing). Only an answer turn holds one; a question
-- never does. Idempotent and append-only.

BEGIN;

ALTER TABLE chat ADD COLUMN IF NOT EXISTS generator TEXT;

ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_generator;
ALTER TABLE chat ADD CONSTRAINT chat_generator CHECK (
    generator IS NULL OR (generator IN ('claude', 'demo') AND role = 'assistant')
);

COMMENT ON COLUMN chat.generator IS
    'claude or demo: what the deployment''s GENERATOR was when this answer turn was written; '
    'NULL on a question and on a turn written before 0020 (#292)';

COMMIT;
