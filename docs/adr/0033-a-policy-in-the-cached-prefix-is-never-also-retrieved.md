# ADR-0033 — A policy in the cached prefix is never also retrieved

- Status: accepted (the human's decision on #156, 2026-10-14)
- Date: 2026-10-14
- Issue: #156
- Amends: `D-11` (`DECISIONS.md`), ADR-0012

## Context

D-11 sends the tenant's policy corpus as the cached prefix of every answer and
retrieves the law. Track B also retrieves the tenant's own documents, policies
included, so the same policy clauses reached the model twice: once in the
prefix and again as retrieved context. They took retrieval slots meant for law.

Reviewing #149 measured it on the fake. Of the top ten for "What must Acme do
to protect the personal information it holds from misuse and loss?", seven
were Acme policy clauses already in the prefix, which left three for law.

## Decision

**The prefix is the policies' authoritative copy, and retrieval leaves out
every clause already in it.**

- **What `answer()` passes.** It hands retrieval the clause ids of the prefix
  it sends, as a `PolicyCorpus` (an interface value beside `LawScope`).
- **Which channels are filtered.** Every `RetrievalStore` method leaves those
  clauses out, on every channel: dense, sparse, fused and the exact-ID lookup.
- **Where the filter runs.** Inside the store's filter, so the freed slots go
  to the next law candidates before fusion's limit applies. Dropping them
  afterwards would only have left the slots empty.
- **What it excludes.** Tenant-scoped chunks only. The prefix holds internal
  policies alone, so a law clause id named in it narrows nothing.
- **What stays retrievable.** External uploads are never in the prefix
  (CONTEXT.md: Document class), so they stay retrievable. The filter is "not
  in this prefix", not "not the tenant's".

**A policy stays citable.** The verifier's context is still the retrieved set
plus the prefix, and the prefix carries every in-force policy clause.

## Measured

Each golden question that isn't a clause id (17 of them) was asked as Acme
subscribed to the Act, over the whole library. Qdrant was real, with real
cached `gemini-embedding-001` vectors and no reranker
(`tests/test_prefix_not_retrieved.py`).

- **Law in the top ten: 64–65 before, 170 after.** Every question's top ten
  is now law. Seven questions went from no law, or one clause, to ten. Before
  varies by a clause between runs, for the reason below.
- **The answer path now retrieves what the library does.** All of Acme's own
  documents are in the prefix, so with the prefix left out Acme sees exactly
  the `_public` view: the same fused scores and the same clauses above the
  tenth. That view is the one the recall gate measures. The answer path's law
  recall is therefore the gate's own, pinned per question in
  `recall_baseline.json`, and the test asserts the two match.
- **Recall@10 of the law each label expects**, over the 9 questions whose
  labels name law:
  - **Mean:** 0.870–0.907 before, 0.870 after.
  - **Before** moves because GQ-18's second expected clause sits on a tie at
    the tenth place, so some runs have it.
  - **After,** GQ-18 is 0.333, its baseline figure. The tie is gone because
    the competing policy candidate is gone, and other law clauses fill the
    places.
  - **The trade-off:** leaving the prefix out puts law in the policies'
    places. It doesn't promise that one particular law clause rises. A clause
    that sat level with a policy can be overtaken by other law.
- **On the fake:** #149's question went from three law clauses of ten to four
  of four. Four is every in-scope law clause its word-overlap scoring matches.

**Qdrant orders candidates with equal RRF scores differently from call to
call.** The test compares tied places by score, not by clause. The adapter
doesn't sort its fused list, while the fake sorts by score then id. That
instability is a separate finding (#160).

## Consequences

- **The recall gate is unchanged, and its baseline stands.** The gate
  measures `route()` for a question with no prefix (PHASES.md's exit bars),
  and that path doesn't change. The answer path's retrieval was re-measured
  above, and it now equals the gate's.
- **A policy-focused question now retrieves law, not more policy.** That is
  the intent: the model already has the whole policy in the prefix. It also
  means retrieval no longer signals which policy clauses best match the
  question. Nothing used that signal, but a later feature that wants it (say,
  highlighting the matching policy clause) has to rank the prefix itself.
- **`PolicyCorpus` is optional where `LawScope` is required.** Leaving it out
  can't widen what a tenant sees. It only spends places on clauses the model
  already has. Making it required would touch every retrieval call, the
  recall gate and the benchmarks, none of which sends a prefix. `answer()`,
  the one caller that sends a prefix, always passes it.
- **`route()` carries it too,** but `answer()` calls `retrieve()`, so a
  question that is a clause id skips the router's keyword lookup (D-12). That
  is older than this change and is #159.
- **The filter grows with the prefix.** It is one `MatchAny` over the prefix's
  clause ids, at most a few hundred under D-11's 200k-token cap.
