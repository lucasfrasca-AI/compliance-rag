-- The findings a corpus revision changes the ground under (#178, ADR-0042).
--
-- count_findings_citing() (0005) counts one tenant's findings by stable-id
-- prefix: the right question for a tenant's own document. Library law is cited
-- by every tenant, and a revision names exact clauses, so this counts the
-- findings of any tenant with at least one citation of one of p_clause_ids,
-- by exact clause_id (a prefix would count s6/10 for s6/1). Kept in the
-- database for the same reason as 0005's: proven against real Postgres, not
-- first written inside an adapter. Mirrors
-- FakeLedgerStore.count_findings_citing_clauses.

BEGIN;

CREATE OR REPLACE FUNCTION count_findings_citing_clauses(p_clause_ids TEXT[])
RETURNS INTEGER
LANGUAGE sql STABLE AS $$
    SELECT count(*)::INTEGER
    FROM finding f
    WHERE EXISTS (
        SELECT 1
        FROM jsonb_array_elements(f.citations) AS c
        WHERE (c ->> 'clause_id') = ANY (p_clause_ids)
    )
$$;

COMMIT;
