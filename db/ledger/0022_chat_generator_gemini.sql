-- A third generator an answer turn can record (#326, ADR-0057): `gemini`.
-- Generation moved from Claude to Gemini 3.5 Flash on Vertex, and a turn says
-- for itself what wrote it (0020), so the column must admit the new name.
-- `claude` stays admitted: every answer turn already written while Claude
-- generated keeps its value, and a chat is read back long after the deployment
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
    'claude, gemini or demo: what the deployment''s generator was when this answer turn was '
    'written (claude until ADR-0057, gemini after); NULL on a question and on a turn written '
    'before 0020 (#292, #326)';

COMMIT;
