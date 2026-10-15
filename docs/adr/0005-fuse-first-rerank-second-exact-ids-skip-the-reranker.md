# ADR-0005 — Fuse first, rerank second; exact IDs skip the reranker

- Status: accepted. Amended by ADR-0046 (#177): Qdrant scores each channel,
  and the retrieval store cuts each channel's list and fuses them with RRF
  itself, not inside Qdrant
- Date: 2026-09-20
- Decision ID: `D-04` (`DECISIONS.md`)

## Context

Cross-encoder cost and latency scale with the number of candidates scored. Reranking everything is wasteful; reranking too few loses recall. Separately, a query that is literally a clause ID has exactly one correct answer.

## Decision

Channels fuse first with RRF inside Qdrant to about 50 candidates, then a cross-encoder reranks those to 10. A clause-ID regex routes exact-ID queries to the keyword channel and skips reranking entirely.

## Consequences

- Rerank cost is bounded by the fusion width, which is one tunable number.
- Exact-ID lookups are fast and cannot be degraded by a semantic scorer.
- Fusion width and rerank depth become eval parameters, measured on the golden set rather than guessed.
