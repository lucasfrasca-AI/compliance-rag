-- Chat history in the ledger (#162; CONTEXT.md: Chat).
--
-- `chat` has been in the schema since v1 and nothing ever wrote to it, so it
-- holds no row anywhere, and every constraint below is validated now, not
-- NOT VALID as 0013's were: there is no earlier row to look back at, and one
-- that somehow existed would fail this migration loudly rather than be
-- tolerated. (As 0009 reshaped `evidence`, for the same reason.)
--
-- A chat is one member's conversation. It grows by exchanges, two rows written
-- in one transaction: the member's question (role 'user', an even turn_index)
-- and what the member was shown in answer (role 'assistant', the odd one
-- after). Append-only, like evidence: a chat is never updated, and nothing
-- here deletes one. A member deleting a chat is its own decision, as purge is
-- (ADR-0021).
--
-- An answer turn holds what the member was shown and nothing else (ADR-0003):
--   verified   the verified text, in `content`, and at least one citation
--              (clause id and version, so it stays clickable after the clause
--              is superseded, ADR-0021);
--   blocked    withheld because a citation didn't match its clause: the blocked
--              clause ids in `blocked`, and no text;
--   uncited    withheld because the answer cited nothing: no text;
--   residency / malformed_generation / internal
--              the request failed: a marker only, no text.
-- `outcome` is NULL on a question. The model's unverified draft has no column,
-- and `content` is NULL for every outcome but 'verified', so a withheld
-- answer's turn cannot hold the prose a bad citation may repeat.
--
-- user_id is the member's uid (ADR-0036), not an email, and every turn of a
-- chat carries the same one: a chat is its member's alone. The database cannot
-- say that across rows, so the adapter checks it under a per-chat lock.

BEGIN;

ALTER TABLE chat ADD COLUMN IF NOT EXISTS outcome   TEXT;
ALTER TABLE chat ADD COLUMN IF NOT EXISTS citations JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE chat ADD COLUMN IF NOT EXISTS blocked   JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE chat ADD COLUMN IF NOT EXISTS as_of     DATE  NOT NULL;

-- Only a verified answer, and a question, have text.
ALTER TABLE chat ALTER COLUMN content  DROP NOT NULL;
-- Every turn names its member, its request and the date the request read the
-- law on (CLAUDE.md: every ledger row that results from a request stores its
-- trace_id).
ALTER TABLE chat ALTER COLUMN user_id  SET NOT NULL;
ALTER TABLE chat ALTER COLUMN trace_id SET NOT NULL;

-- 'system' was allowed in v1 and nothing writes it: a chat holds what the
-- member asked and what they were shown.
ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_role_check;
ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_role;
ALTER TABLE chat ADD CONSTRAINT chat_role CHECK (role IN ('user', 'assistant'));

-- Questions at even indices, answers at odd: turns come in exchanges.
ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_in_exchanges;
ALTER TABLE chat ADD CONSTRAINT chat_in_exchanges
    CHECK (turn_index >= 0 AND (turn_index % 2 = 0) = (role = 'user'));

ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_user_id;
ALTER TABLE chat ADD CONSTRAINT chat_user_id CHECK (btrim(user_id) <> '');

ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_trace_id;
ALTER TABLE chat ADD CONSTRAINT chat_trace_id CHECK (btrim(trace_id) <> '');

ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_outcome;
ALTER TABLE chat ADD CONSTRAINT chat_outcome CHECK (outcome IN (
    'verified', 'blocked', 'uncited', 'residency', 'malformed_generation', 'internal'
));

-- What each kind of turn may hold. A CASE, because SQL evaluates AND's operands
-- in any order and jsonb_array_length raises on a scalar: the array check must
-- come first, and only a CASE promises that. A NULL here would pass a CHECK,
-- so every branch names what must be present as well as what must be absent.
ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_turn_shape;
ALTER TABLE chat ADD CONSTRAINT chat_turn_shape CHECK (
    CASE
        WHEN jsonb_typeof(citations) IS DISTINCT FROM 'array'
          OR jsonb_typeof(blocked) IS DISTINCT FROM 'array'
            THEN FALSE
        -- the member's question
        WHEN role = 'user'
            THEN outcome IS NULL
             AND content IS NOT NULL AND btrim(content) <> ''
             AND jsonb_array_length(citations) = 0 AND jsonb_array_length(blocked) = 0
        -- an answer always says how it ended
        WHEN outcome IS NULL
            THEN FALSE
        WHEN outcome = 'verified'
            THEN content IS NOT NULL AND btrim(content) <> ''
             AND jsonb_array_length(citations) > 0 AND jsonb_array_length(blocked) = 0
        WHEN outcome = 'blocked'
            THEN content IS NULL
             AND jsonb_array_length(citations) = 0 AND jsonb_array_length(blocked) > 0
        -- uncited, and every way a request fails: a marker, nothing else
        ELSE content IS NULL
             AND jsonb_array_length(citations) = 0 AND jsonb_array_length(blocked) = 0
    END
);

-- A member's chats, for the list.
CREATE INDEX IF NOT EXISTS chat_by_member ON chat (tenant_id, user_id, chat_id);

COMMENT ON COLUMN chat.user_id IS
    'the member''s uid (ADR-0036), never an email; one per chat (#162)';
COMMENT ON COLUMN chat.content IS
    'the question, or a verified answer''s text; NULL for a withheld or failed answer (#162)';
COMMENT ON COLUMN chat.outcome IS
    'how an answer ended: what the member was shown; NULL on a question (#162)';
COMMENT ON COLUMN chat.citations IS
    'a verified answer''s citations: [{clause_id, version_id}], never the model''s quote';
COMMENT ON COLUMN chat.blocked IS
    'a blocked answer''s blocked clause ids: [clause_id, ...]';
COMMENT ON COLUMN chat.as_of IS 'the date the request read the law on';

COMMIT;
