-- A second reason a model can be unavailable (#322, ADR-0056): `access`. Bedrock
-- refuses the caller rather than the load when access is denied, the model is
-- not enabled for the account, the first-time-use form or the Marketplace
-- subscription is not complete, or the credentials are not accepted. Until now
-- the only reason a `generation_unavailable` turn could hold was `quota`
-- (0019); this widens that one list, and nothing else.
--
-- The detail keeps 0019's rule: exactly the keys named, no free text, so the
-- provider's words can't be stored in it. Every existing row keeps its outcome
-- and its detail. Idempotent and append-only: 0019's constraint is replaced by
-- the same constraint with one more admitted reason.

BEGIN;

ALTER TABLE chat DROP CONSTRAINT IF EXISTS chat_outcome_detail;
ALTER TABLE chat ADD CONSTRAINT chat_outcome_detail CHECK (
    CASE
        WHEN outcome = 'generation_unavailable' OR outcome = 'policy_corpus_too_large'
            THEN CASE
                WHEN jsonb_typeof(outcome_detail) IS DISTINCT FROM 'object'
                    THEN FALSE
                WHEN outcome = 'generation_unavailable'
                    THEN (outcome_detail - 'reason' = '{}'::jsonb
                     AND outcome_detail->>'reason' IN ('quota', 'access')) IS TRUE
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
    'what a refused generation or an over-cap corpus was told: {reason} (quota or access, our '
    'word, never the provider''s message) for generation_unavailable, {estimated_tokens, '
    'cap_tokens} for policy_corpus_too_large (ADR-0054); NULL on every other turn (#289, #322)';

COMMIT;
