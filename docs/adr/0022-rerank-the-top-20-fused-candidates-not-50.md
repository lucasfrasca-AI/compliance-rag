# ADR-0022 — Rerank the top 20 fused candidates, not ~50

- Status: accepted
- Date: 2026-10-08
- Decision ID: revises `D-04` (`DECISIONS.md`)
- Evidence: `docs/research/reranker-benchmark.md` (issue #32)

## Context

D-04 fixed the shape — fuse first with RRF, rerank second with a
cross-encoder down to 10, exact clause-ID queries skip reranking — and put
"~50" on the number of fused candidates handed to the reranker. That number
was a convention, never measured. On the deployed service (int8
`bge-reranker-v2-m3`, Cloud Run, 2 vCPU) a 50-candidate rerank took ~16 s
median warm and ~37 s cold (BUILD_LOG 2026-10-06), and every lever left to
pull traded quality for latency, which nothing could measure until the
golden set existed (#27) and the recall gate ran (#28).

## Decision

**Rerank the top 20 fused candidates** (`retrieval.pipeline.RERANK_CANDIDATES`).
Keep the cross-encoder (`bge-reranker-v2-m3`, int8) and everything else in D-04.

Measured on the signed golden set, real `gemini-embedding-001` vectors, real
Qdrant, recall@10 per category, against PHASES.md's exit bars (exact-ID 1.0,
paraphrase 0.9, cross-clause 0.8):

| configuration | exact | paraphrase | cross-clause | Cloud Run warm, median / max of 8 |
|---|---|---|---|---|
| no rerank (fusion only) | 1.0 | 1.0 | **0.792** ✗ | — |
| v2-m3 over top 50 | 1.0 | 1.0 | 0.938 | 16.1 s / 19.1 s |
| **v2-m3 over top 20** | 1.0 | 1.0 | **0.896** | **6.4 s / 7.7 s** |
| bge-reranker-base over top 50 | 1.0 | **0.889** ✗ | 0.958 | not deployed; ~4 s *projected* from local timing |
| bge-reranker-base over top 20 | 1.0 | **0.889** ✗ | 0.896 | not deployed; ~2 s *projected* from local timing |

Top 20 is the only configuration that clears every bar at a latency within
reach. With 8 warm samples the maximum is a sample maximum, not a measured
p95; cold start is a single request (28.3 s at 20 candidates). Fusion alone misses the cross-clause bar; top 50 buys 0.04 of
cross-clause recall for ~2.5x the latency; the smaller model is fastest but
drops a paraphrase query entirely.

## Consequences

- The cross-clause exit bar is now enforced on what `retrieve()` delivers —
  the reranked list — in CI's `reranker-fidelity` job
  (`tests/test_recall_gate_reranked.py`). The fused gate keeps exact-ID and
  paraphrase on every build, with no model, and holds every query to its
  baseline.
- The golden set is 25 queries (9 paraphrase, 8 cross-clause): one query
  moves a category by 0.11–0.13. These numbers choose between configurations
  that differ by more than that; they are not a precise estimate. Revisit
  when the golden set grows (PHASES.md: expanded when a real failure shows a
  gap).
- Cold start is unchanged by this decision in kind — a scale-to-zero
  container still loads the model on first request — and is reported
  separately in the benchmark.
- Changing the reranker model would change the chunk budget (`chunker.py`
  derives it from the reranker's window); staying on v2-m3 avoids that.
