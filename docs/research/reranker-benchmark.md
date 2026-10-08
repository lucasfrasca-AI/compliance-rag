# Reranker benchmark

Issue #32 · 2026-10-08 · decision: **ADR-0022** (rerank the top 20 fused candidates with the deployed `bge-reranker-v2-m3` int8)

## Question

Is reranking worth its cost, and at what candidate count and model size? #23 measured a 50-candidate rerank at ~16–19 s warm and ~37 s cold on the deployed service. Every remaining lever trades quality for latency, and the golden set (#27) is the first instrument that can measure quality.

ColBERT is out of scope: #26 was closed as deferred because of a structural window mismatch.

## Method

**Quality.** Recall@10 per golden-set category against PHASES.md's exit bars:

| category | bar | queries |
|---|---|---|
| exact-ID | 1.0 | 8 |
| paraphrase | ≥ 0.9 | 9 |
| cross-clause | ≥ 0.8 | 8 |

The harness is the recall gate's own (#28, `scripts/benchmark_reranker_recall.py`):
- the signed golden set on a real Qdrant server;
- real `gemini-embedding-001` vectors (checked-in snapshot, drift 1.000000 on 2026-10-08);
- local BM25 and RRF fusion through `route()`;
- the reranker's own FastAPI app behind the real `RerankerClient` over in-process ASGI, so it runs the production code path.

Exact-ID queries take the router's lookup and never reach a reranker (D-04), so they score the same in every arm.

**Latency.** The deployed `reranker` Cloud Run service (`australia-southeast1`, 2 vCPU, `intra_op_num_threads=2`), called as `compliance-rag-runtime` (`scripts/benchmark_reranker_latency.py`).
- **Batches:** each golden paraphrase or cross-clause query's own fused candidates, cut to 50 and to 20.
- **Warm:** one uncounted warm-up request, then 8 queries, alternating the 50- and 20-candidate batches. The 8 queries are the first 8 non-exact entries (GQ-09 to GQ-16), all paraphrase. Batch cost is dominated by candidate count and length, which come from the same corpus for every category, but cross-clause batches weren't timed separately. With 8 samples, the maximum is a sample maximum, not a measured p95.
- **Cold:** one request after 22 minutes with no traffic, beyond the service's ~17-minute idle-to-zero window. It is reported on its own, never averaged into warm.

**Candidate model.** `BAAI/bge-reranker-base@2cfc18c9` (278M parameters), exported with the same optimum-ONNX and `quantize_dynamic` QInt8 recipe as production (`scripts/export_candidate_reranker.py`) and run through the identical harness and thread pinning. It was not deployed. Its Cloud Run latency is *projected* from the local ratio: v2-m3 over 20 candidates is 4.12 s locally and 6.42 s on Cloud Run, a factor of 1.56.

`mxbai-rerank-base-v2` was not run. It is a Qwen2-based LLM-style reranker, not a sequence-classification cross-encoder, so the existing export path, service and chunk budget (derived from an XLM-R 512-token window) don't apply. Evaluating it means building a new serving path first. That is a gap in this benchmark, recorded rather than closed. It's worth closing only if latency, rather than quality, becomes the binding constraint.

## Results

### Recall@10

| configuration | exact-ID | paraphrase | cross-clause | misses |
|---|---|---|---|---|
| no rerank (fusion only) | 1.000 | 1.000 | **0.792** ✗ | GQ-18 (1/3), GQ-20 (1/2), GQ-23 (1/2) |
| v2-m3 over top 50 | 1.000 | 1.000 | 0.938 | GQ-20 (1/2) |
| **v2-m3 over top 20** | 1.000 | 1.000 | **0.896** | GQ-18 (2/3), GQ-20 (1/2) |
| base over top 50 | 1.000 | **0.889** ✗ | 0.958 | GQ-16 (0/1), GQ-18 (2/3) |
| base over top 20 | 1.000 | **0.889** ✗ | 0.896 | GQ-16 (0/1), GQ-18 (2/3), GQ-20 (1/2) |

### Latency

| configuration | warm median | warm max of 8 | cold (n=1) | local median |
|---|---|---|---|---|
| no rerank | 0 | 0 | 0 | 0 |
| v2-m3 over top 50 | 16.07 s | 19.10 s | 37.2 s (2026-10-06) | 9.26 s |
| **v2-m3 over top 20** | **6.42 s** | **7.74 s** | **28.29 s** | 4.12 s |
| base over top 50 | ~4.1 s (projected) | — | not measured | 2.62 s |
| base over top 20 | ~1.9 s (projected) | — | not measured | 1.20 s |

### Top-1, and the APP 10.2 data point

#25's test, on fake 8-dimension vectors, had the int8 model rank APP 10.2 above APP 11.2 for "what must an entity do with personal information it no longer needs?". On real embeddings that doesn't happen. For GQ-10 (the same question in other words), the top-1 is APP 11.2(b) under fusion, v2-m3 over 20 and v2-m3 over 50 alike.

Top-1 within the expected clauses, over the 17 non-exact queries:

| configuration | top-1 within expected |
|---|---|
| fusion | 10 of 17 |
| v2-m3 over 20 | 10 of 17 |
| v2-m3 over 50 | 9 of 17 |

The cross-encoder's gain is in which clauses make the top 10, which is what the exit bars measure, not in which clause comes first.

### Determinism, and what wasn't tested

On real embeddings, two identical runs of v2-m3 over top 50 ranked all 25 of 25 queries identically. The run-to-run flips in #25's test came from fake vectors full of tied scores, which changed the batch the int8 model saw (BUILD_LOG 2026-10-08).

This shows determinism for a fixed batch. **It does not test batch dependence:** whether a clause's score shifts when the other candidates change, as they do between 50 and 20. Int8 dynamic quantization scales activations over the whole batch, so it can. The recall numbers per configuration already include whatever effect that has, but the effect isn't isolated here.

## Reading it

- **Fusion alone fails the cross-clause bar.** RRF ranks a clause that only one channel finds below clauses both channels find. For example, GQ-18's APP 11.1(b) is 5th in dense and 11th fused. Reranking is what fixes it, so reranking stays.
- **Top 20 keeps every bar at about 40% of the warm latency of top 50.** The 0.04 of cross-clause recall that top 50 adds is one clause of one query (GQ-18), and it costs about 10 s per request warm.
- **The smaller model is the fastest, but it loses a paraphrase query outright.** GQ-16 asks "Can Acme use my purchase history to send me promotional emails without asking first?" and expects APP-aligned consent clause `acme/privacy-policy:v1:3/2`; recall is 0 even over 50 candidates. That fails the paraphrase bar. Its cross-clause lead (0.958) doesn't offset a bar failure.
- **Sample size limits what this proves.** One query moves paraphrase by 0.11 and cross-clause by 0.125. The numbers choose between configurations whose differences exceed that, and they are not precise estimates. Re-run when the golden set grows.

## Decision

The default is the **cross-encoder (`bge-reranker-v2-m3`, int8) over the top 20 fused candidates** (`RERANK_CANDIDATES = 20`). It revises D-04's "~50" (ADR-0022) and is enforced by `tests/test_recall_gate_reranked.py` in CI's `reranker-fidelity` job.

## What it leaves open

- **Cold start is 28 s.** That's container boot plus model load: about 22 s over warm, consistent with a 23 s single-document cold probe. Candidate count can't fix it. The levers are min-instances=1 (~$8–15/mo, a SERVICES.md change) or a smaller image and model. That decision belongs to the human, and it gets clearer once real request patterns exist (Phase 3).
- **Warm 6–8 s per rerank** is the remaining latency floor of a 568M-parameter cross-encoder on 2 vCPU. With no generation in the path yet, it is most of `retrieve()`.
- **`bge-reranker-base` stays the obvious next candidate** if latency matters more than GQ-16-style paraphrases. A larger golden set would show whether GQ-16 is a pattern or a single case.
