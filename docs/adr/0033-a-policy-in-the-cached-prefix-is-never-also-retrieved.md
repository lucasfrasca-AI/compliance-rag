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
  it sends (`in_prefix`).
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
cached `gemini-embedding-001` vectors and no reranker.

**Law chunks in the top ten went from 65 to 170.** No question lost law,
because RRF scores a candidate by its own rank in each channel, so removing
others can only move law up. Five questions went from no law at all to ten
law clauses. On the fake, #149's question went from three law clauses to
four, which is every in-scope law clause its word-overlap scoring matches.

## Consequences

- **The recall gate is unchanged, and its baseline stands.** The gate
  measures `route()` for a question with no prefix (PHASES.md's exit bars),
  and that path doesn't change. What changes is `answer()`'s retrieval, and
  the measurement above re-measures that.
- **A policy-focused question now retrieves law, not more policy.** That is
  the intent: the model already has the whole policy in the prefix. It also
  means retrieval no longer signals which policy clauses best match the
  question. Nothing used that signal, but a later feature that wants it (say,
  highlighting the matching policy clause) has to rank the prefix itself.
- **The filter grows with the prefix.** It is one `MatchAny` over the prefix's
  clause ids, at most a few hundred under D-11's 200k-token cap.
