-- Two more ways an answer turn can end (#289), so a chat read back says what
-- the member was shown. Until now both were recorded as `internal`:
--
--   generation_unavailable    the model refused the request (the 429 quota
--                             case, #251). Kept with its reason, `quota`, and
--                             nothing the provider said;
--   policy_corpus_too_large   the CAG prefix was over the cap (ADR-0054),
--                             refused before any model was called. Kept with
--                             the estimate and the cap, as the stream event
--                             carries them.
--
-- Both are failures, so a marker as 0014 defines it: no text, no citations,
-- nothing blocked, and (0018) no named-instrument flag. The detail lives in
-- one nullable JSONB column, `outcome_detail`, which only these two outcomes
-- hold. Every existing row keeps its outcome (a turn recorded as `internal`
-- stays `internal`) and takes NULL. Idempotent and append-only.

BEGIN;

ALTER TABLE chat ADD COLUMN IF NOT EXISTS outcome_detail JSONB;

ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_outcome;
ALTER TABLE chat ADD CONSTRAINT chat_outcome CHECK (outcome IN (
    'verified', 'blocked', 'uncited', 'residency', 'malformed_generation', 'internal',
    'generation_unavailable', 'policy_corpus_too_large'
));

-- 0018's rule: a question and a failed request hold no flag. The failures are
-- now five.
ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_not_in_environment;
ALTER TABLE chat ADD CONSTRAINT chat_not_in_environment CHECK (
    CASE
        WHEN jsonb_typeof(not_in_environment) IS DISTINCT FROM 'array'
            THEN FALSE
        WHEN role = 'user' OR outcome IN (
            'residency', 'malformed_generation', 'internal',
            'generation_unavailable', 'policy_corpus_too_large'
        )
            THEN jsonb_array_length(not_in_environment) = 0
        ELSE TRUE
    END
);

-- What the detail may hold: exactly the keys named, numbers that are whole and
-- not negative, and no free text, so the provider's words can't be stored in
-- it. A CASE, as 0014's chat_turn_shape: `jsonb - text` raises on a scalar, so
-- the object check must come first, and only a CASE promises the order.
-- `IS TRUE`, because a CHECK passes on NULL, and a missing key makes the
-- expression NULL, not false.
ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_outcome_detail;
ALTER TABLE chat ADD CONSTRAINT chat_outcome_detail CHECK (
    CASE
        WHEN outcome = 'generation_unavailable' OR outcome = 'policy_corpus_too_large'
            THEN CASE
                WHEN jsonb_typeof(outcome_detail) IS DISTINCT FROM 'object'
                    THEN FALSE
                WHEN outcome = 'generation_unavailable'
                    THEN (outcome_detail - 'reason' = '{}'::jsonb
                     AND outcome_detail->>'reason' IN ('quota')) IS TRUE
                ELSE (outcome_detail - 'estimated_tokens' - 'cap_tokens' = '{}'::jsonb
                     AND jsonb_typeof(outcome_detail->'estimated_tokens') = 'number'
                     AND jsonb_typeof(outcome_detail->'cap_tokens') = 'number'
                     AND (outcome_detail->>'estimated_tokens') ~ '^[0-9]+$'
                     AND (outcome_detail->>'cap_tokens') ~ '^[0-9]+$') IS TRUE
            END
        ELSE outcome_detail IS NULL
    END
);

COMMENT ON COLUMN chat.outcome_detail IS
    'what a refused generation or an over-cap corpus was told: {reason} for generation_unavailable '
    '(our word, never the provider''s message), {estimated_tokens, cap_tokens} for '
    'policy_corpus_too_large (ADR-0054); NULL on every other turn (#289)';
COMMENT ON COLUMN chat.outcome IS
    'how an answer ended: what the member was shown; NULL on a question (#162); '
    'generation_unavailable and policy_corpus_too_large added by 0019 (#289)';

COMMIT;
