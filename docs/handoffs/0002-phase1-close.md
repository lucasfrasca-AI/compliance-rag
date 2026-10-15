# Handoff 0002 — Phase 1 close

- **Date:** 2026-10-10.
- **Phase:** 1, Corpus, retrieval, verifier.
- **Status:** **not closed.** Every exit criterion is built and every number but two is measured. The verifier pass rate on the golden set and the measured cost per query wait on one external step: a quota increase for Claude at Vertex's US multi-region endpoint (see *The one blocker*). The "Current phase" line in `PHASES.md` is deliberately unchanged.

## Read first

- **Before acting:** `CLAUDE.md`, `PHASES.md` Phase 1 (exit criteria with measured values inline), `DECISIONS.md` (D-04, D-07 and D-16 were amended this phase), and `SERVICES.md`. SERVICES.md's Claude row and the new AU-generation row both changed this phase.
- **For what is built and how it is tested:** `docs/handoffs/0001-phase1-architecture-and-status.md`. Its last section, *Model-swap consequences*, is the planning note for the residency decision. It is summarised again at the end of this document.

## The one blocker

`claude-sonnet-5` is enabled for project `compliance-rag-508904`, but every call at the `us` multi-region endpoint returns:

> 429 Quota exceeded for `aiplatform.googleapis.com/us_multi_region_online_prediction_requests_per_base_model` with base model: anthropic-claude-sonnet. Please submit a quota increase request.

It fails on the first request and again after idle periods, on 2026-10-10 at 01:2x and 14:20, so the quota is effectively zero, not a burst limit. A person has to request the increase in the console. The Cloud Quotas API isn't enabled for the project, so the value can't be read from here, and enabling it is also a human step.

When the quota exists, run these with your `.env` (`VERTEX_CLAUDE_MODEL=claude-sonnet-5`, `VERTEX_CLAUDE_REGION=us`; an unset region means Sydney, which fails closed):

```
QDRANT_URL=http://localhost:6333 uv run python scripts/run_answer_eval.py
QDRANT_URL=http://localhost:6333 uv run python scripts/run_answer_eval.py --injection
QDRANT_URL=http://localhost:6333 uv run python scripts/demo_answer.py
```

The first writes `corpus/golden/answer_eval/<date>-claude-sonnet-5.json` with a `summary`:
- verifier pass rate (answers and citations);
- schema-valid rate;
- mean token sizes;
- cache hits on the prefixed requests;
- mean cost per query;
- the 2,000-query projection, with its arithmetic.

Commit that file, and `tests/test_recorded_answers.py` re-verifies every recorded citation on every build with no LLM. Then:
- close #31 and #29;
- update `PHASES.md`'s exit criteria with the measured values;
- change the "Current phase" line in the same commit.

## Exit criteria — where each stands

| Criterion | Status | Evidence |
|---|---|---|
| recall@10 exact-ID = 1.0, paraphrase ≥ 0.9, cross-clause ≥ 0.8 | **Met** | On what `retrieve()` delivers (cross-encoder over the top 20): **1.000 / 1.000 / 0.896**. Enforced in CI's `reranker-fidelity` job (`tests/test_recall_gate_reranked.py`). The fused gate holds exact-ID and paraphrase plus per-query baselines on every build. Fusion alone scores 0.792 on cross-clause; dense alone 0.896 |
| Verifier pass rate 100% on the golden set, with a deliberately bad citation proven to block | **Bad citation: met. Pass rate: blocked** | `test_a_deliberately_bad_citation_blocks`. A retired or withdrawn version blocks even when retrieval leaks it (ADR-0021 mechanism 3); a verifier blind to the ledger fails those tests. The golden-set rate needs the live run |
| Cost per query logged; projected < $60/month at 2,000 | **Logging met. Number blocked** | One ledger `usage` row per generation call, with real tokens, cost and `trace_id`, on Postgres and the fake. The projection code is pinned by a test. Estimate until measured (below) |
| Reranker benchmark checked in; winner is the default | **Met** | `docs/research/reranker-benchmark.md`, ADR-0022 |

## Every number from the phase

**Retrieval (golden set: 25 queries, signed off by Lucas Frasca on 2026-10-08)**

recall@10 per configuration:

| Configuration | exact | paraphrase | cross-clause |
|---|---|---|---|
| fused | 1.000 | 1.000 | 0.792 |
| dense alone | – | 1.000 | 0.896 |
| sparse alone | – | 0.889 | 0.729 |
| v2-m3 over 50 | 1.000 | 1.000 | 0.938 |
| **v2-m3 over 20 (default)** | 1.000 | 1.000 | **0.896** |
| `bge-reranker-base` over 50 | 1.000 | 0.889 ✗ (misses GQ-16) | 0.958 |
| `bge-reranker-base` over 20 | 1.000 | 0.889 ✗ | 0.896 |

- **Top-1 within expected:** fusion 10/17, v2-m3 over 20 10/17, over 50 9/17.
- **Determinism:** 25/25 queries ranked identically across two runs on real embeddings.
- **The APP 10.2-over-11.2 top-1** seen in #25's test came from fake 8-dimension vectors and doesn't occur on real embeddings.

**Reranker on Cloud Run** (2 vCPU, int8 `bge-reranker-v2-m3`, threads pinned 2/1)

| Batch | Warm median | Warm max of 8 | Cold (n=1) |
|---|---|---|---|
| 20 candidates | 6.42 s | 7.74 s | 28.29 s |
| 50 candidates | 16.07 s | 19.10 s | 37.2 s |

Cold start is about 22 s of container boot and model load on top of the warm time. Candidate count doesn't change it; min-instances does (a SERVICES.md decision).

**Embeddings**
- **Snapshot:** 137 golden texts embedded once, ≈ $0.0007, checked in as `gemini-embedding-001`, RETRIEVAL_DOCUMENT, google-cloud-aiplatform 1.165.1, 2026-10-08.
- **Drift:** `scripts/check_embedding_drift.py` reported a minimum cosine of 1.000000 over 10 texts.
- **SDK:** #73 tracks the Vertex SDK's announced removal of `TextEmbeddingModel`.

**Generation (measured cost pending)**
- **Model:** `claude-sonnet-5` at the US multi-region.
- **Rates** (Google's pricing page, US Multi-Region tab), per million tokens: $2.20 input, $11.00 output, $2.75 for a 5-minute cache write, $0.22 for a cache hit.
- **Estimate, not a measurement:** about 2,000 uncached input tokens, about 5,000 cached prefix tokens and about 400 output tokens per query come to about **$0.0099 a query**, or about **$20 a month at 2,000 queries**. The live run replaces this with measured sizes.

**Probes this phase (all $0)**
- Claude in `australia-southeast1`: 404.
- Sonnet 4.6 and 4.5 at `asia-southeast1`: 404 (not enabled).
- Sonnet 5 at `us`: 429 (quota).

## Every ADR from the phase

| ADR | Decision |
|---|---|
| 0018 | Policy clauses are projected into the graph per request, never persisted |
| 0019 | Clause-level anchors are synthesized from `clause_path`, not read from source markup |
| 0020 (D-17) | A curated library with per-tenant subscriptions; the filter is built in the adapter from the ledger |
| 0021 (D-18) | Version and retire everything; removal is three explicit states (unsubscribe, withdraw, purge) |
| 0022 (revises D-04) | Rerank the top 20 fused candidates, not ~50 (measured) |
| 0023 (corrects D-07, amends D-16) | Claude is not served from Sydney. Build-phase generation runs offshore only while the corpus is public law plus synthetic data, behind a hard gate: refused at startup and before every offshore request once real tenant data exists, real tenant uploads refused at ingest, `global` refused, unmarked tenant chunks count as real, the region defaults to Sydney |
| 0024 (amends D-07, D-16) | Build-phase generation on `claude-sonnet-5` at the `us` multi-region endpoint; allowed regions are exactly {`australia-southeast1`, `us`}. Supersedes 0023's model and region; the gate is unchanged |
| 0025 | The library owner is one seeded `tenant` row (`_public`, `is_library`); subscriptions FK to the library instrument; the library never subscribes |

## What merged this phase's final stretch

| PR | Contents |
|---|---|
| #72 | `retrieve()` on the real adapters (#25) |
| #74 | The recall gate (#28) |
| #75 | The Claude generator, fenced verbatim prompts and the injection test (part of #29) |
| #76 | The citation verifier (#30) |
| #77 | The reranker benchmark and the top-20 default (#32) |
| #79 | The residency gate (ADR-0023) |
| #80 | Sonnet 5 at `us` (ADR-0024) |
| #81 | The library owner (#67) |
| #82 | The Postgres ledger and the shared ledger contract (#69) |
| #83 | Usage rows with `trace_id`, the live eval, recorded re-verification and the demo (part of #31) |

## Open issues

*Updated 2026-10-10, later the same day, after a second run (PRs #85, #86, #90–#92, #94).*

- **#29 and #31:** only the live criteria remain. Both wait on US multi-region Claude quota, which the human has requested. `.env` is now set to `claude-sonnet-5` / `us`.
- **#12:** ISO 27001 Annex A control IDs with our own descriptors. This is a Phase 1 deliverable, not started; it waits on the human's control-ID list.
- **#78 (Phase 3 blocker):**
  - **Done:** the `synthetic` flag is now operator-controlled at the write path (ADR-0026, PR #90).
  - **Still owed:** the API-level test, once Phase 3's upload API exists.
- **#93:** the Phase 2 design session, covering condition valid time, APPLIES_TO targets, controls, corrections versus D-05 immutability, where policy mappings live, and status/date agreement. The graph loader validates these shapes and refuses to load them until the session settles them.
- **#8:** the Phase 1 parent.
- **#73: closed.** The embedder is on `google-genai`. The snapshot was regenerated, and all 137 vectors are byte-identical.

## Phase 2 scaffolding (started early, at the human's instruction)

Merged:
- **#87:** a Neo4j adapter that applies the schema idempotently, plus a Neo4j 5.26.31 Community container in CI. Writes are immutable and bitemporal; a retire takes the node's write lock before reading.
- **#88:** a query builder that requires `as_of` and a depth cap, and applies the bitemporal check to every node on a path. A built query is sealed against tampering. Amendments are one atomic transaction.
- **#89:** graph loaders, which go through a new `GraphAdmin` interface.
  - **Loaded:** corpus structure, obligations, obligation `SUPERSEDES`, and obligation crosswalks.
  - **Not loaded:** anything in #93's scope.
  - **Policy mappings:** never written to the graph (D-09).

**Aura Free:** on 2026-10-10 the hostname `compliance-rag-neo` did not resolve, though DNS works otherwise. So the instance is paused or deleted. Resuming or recreating it is a console step for the human. Nothing built so far needs it, because every graph test runs against the local or CI container.

## Residency and the decision that's coming

- **Where things sit today:** everything is in `australia-southeast1` except two components. Neo4j Aura Free is offshore under D-09 (public law only). Build-phase generation runs at `us` under ADR-0023 and ADR-0024, gated in code to public law plus synthetic data.
- **When the gate closes:** the first real customer document closes it. At that point generation must move to Australia, and there are two options (PHASES.md Phase 5):
  - **GCP:** `gemini-3.5-flash` in Sydney, sold there only as Single Zone Provisioned Throughput, at **~$2,200–3,000 a month** for the one-GSU minimum.
  - **AWS Bedrock:** the AU geo inference profile (`au.anthropic.claude-sonnet-5`) on **pay-per-token** pricing. That keeps the model, but adds a second cloud.

### Model-swap consequences (full text in handoff 0001)

- **Untouched by any swap (D-08):**
  - retrieval, reranking and the recall gates;
  - prompt fencing, draft parsing and the verifier, which never sees the model;
  - `answer()` and the residency gate.
- **Claude to Claude** (a later Sonnet, or Vertex to Bedrock AU): a configuration change for Vertex. For Bedrock it is a new adapter on the same Anthropic Messages contract. Check the model's endpoints first and update the prices. Then re-run the golden answer eval, the injection probes and the cache check.
- **Claude to Gemini:**
  - a new adapter;
  - a different caching shape, implicit or an explicit cached-content resource billed for storage, with no cache-write tokens;
  - structured output and the injection behaviour re-validated live;
  - a new cost model: a fixed monthly commitment, not per-token, so `cost_usd` needs a new meaning.

## Suggested skills for the next session

- `mattpocock-skills:implement` with `tdd` at the seams, for #12 and Phase 2's graph work.
- `mattpocock-skills:code-review` (Standards and Spec as parallel sub-agents) before every PR.
- `mattpocock-skills:grill-with-docs` before Phase 2's first ticket. Track A, obligation filtering and the tenant subgraph are Phase 2's open design questions.
- `mattpocock-skills:handoff` at the end of the session that closes Phase 1.
