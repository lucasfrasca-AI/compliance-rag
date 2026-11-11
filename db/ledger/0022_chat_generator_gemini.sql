-- A third generator an answer turn can record (#326, ADR-0057): `gemini`.
-- Generation moved from Claude to Gemini 3.5 Flash on Vertex, and a turn says
-- for itself what wrote it (0020), so the column must admit the new name.
-- `claude` stays admitted: it is still a backend (dormant until Claude is
-- approved on this project), and answer turns already written while it
-- generated keep their value; a chat is read back long after the deployment
-- has changed. Nothing else about the column changes.
--
-- Idempotent and append-only: 0020's constraint is replaced by the same
-- constraint with one more admitted name. No row is touched.

BEGIN;

ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_generator;
ALTER TABLE chat ADD CONSTRAINT chat_generator CHECK (
    generator IS NULL OR (generator IN ('claude', 'gemini', 'demo') AND role = 'assistant')
);

COMMENT ON COLUMN chat.generator IS
    'claude, gemini or demo: what the deployment''s GENERATION_BACKEND was when this answer '
    'turn was written (gemini by default since ADR-0057; claude is dormant); NULL on a question and on a turn written '
    'before 0020 (#292, #326)';

COMMIT;
