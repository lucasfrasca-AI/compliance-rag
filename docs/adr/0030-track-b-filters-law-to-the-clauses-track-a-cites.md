# ADR-0030 — Track B filters library law to the clauses Track A cites

- Status: accepted
- Date: 2026-10-12
- Issue: #116
- Refines: ADR-0004 / `D-03` ("Track B returns supporting text, filtered to Track A's IDs")

## Context

D-03 filters Track B to Track A's obligation IDs, so a topically similar
paragraph can never stand in for a deterministic finding. Two things stopped
that being real:

- **The filter had a hole.** `RetrievalStore.search(obligation_ids=…)` treated
  an empty tuple as *no filter*. Every caller passed `()`. So did Track A
  finding nothing applicable, which would have meant *all law*.
- **Chunks didn't carry obligations.** Chunks carry an `obligation_id` payload,
  but nothing sets it. Setting it would copy a human mapping into a derived
  store, and it would drift from the graph on every mapping change. It would
  also break when an obligation spans compilations (ADR-0028): one chunk per
  clause version, one obligation across all of them.

## Decision

**A required `LawScope` replaces `obligation_ids` on every retrieval method:**
`RetrievalStore.search` and `lookup_clause`, and
`RetrievalPipeline.retrieve` and `route`.

- **`LawScope.of(coverage)` is the set of clauses Track A cites** for every
  obligation that is applicable or unassessable on `as_of`:
  - the obligations' own clauses;
  - the clauses of the conditions and actors that decided them.

  Library law outside that set is never returned, by any channel: dense,
  sparse, fused, or the keyword channel's exact-ID lookup. The mapping from
  obligation to clause is the graph's `DERIVED_FROM`, read on `as_of`, so the
  filter follows the graph with nothing copied into Qdrant. In Qdrant it is a
  `MatchAny` on the existing keyword-indexed `clause_id` payload.
- **Not applicable means not retrieved.** An obligation that doesn't apply is
  reported in the coverage report but contributes no clause. One that might
  apply (unassessable) does, so a "possibly" answer still cites the law that
  would apply.
- **An empty set means no law.** If Track A finds nothing that might apply,
  Track B returns no library law at all.
- **The tenant's own documents aren't law.** Track A doesn't scope them; the
  subscription filter (ADR-0020) still does.
- **`LawScope.unscoped(reason)` is the only way past it.** It is for a
  question asked with no tenant profile:
  - the Phase 1 recall gate (`eval.recall.WHOLE_LIBRARY`);
  - `answer()`'s library Q&A (`assessment.pipeline.LIBRARY_QUESTION`);
  - tests of retrieval itself.

  It must give a reason, and the reason is on every `retrieve` span
  (`retrieval.law_scope`). An unscoped call is visible in code and in traces,
  never an empty argument.

## Consequences

- D-03 holds by construction. `test_track_b_scope.py` proves it on the fake
  and on real Qdrant: a query that *is* an out-of-scope clause's text gets that
  clause first when unscoped and nowhere when scoped.
- Definitions (s6's `personal information`) aren't in Track A's set unless a
  condition derives from them. A scoped answer cites the obligation and its
  reasons, not the definitions around them. If that proves too narrow, the fix
  is to derive the relevant conditions from those definitions in mapping YAML.
  Unscoping is not the fix.
- The `obligation_id` payload on chunks is no longer read by any filter.
