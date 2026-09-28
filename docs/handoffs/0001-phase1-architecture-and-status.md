# Handoff 0001 — Phase 1 architecture and build status

**Date:** 2026-09-28 (updated; originally written 2026-09-27)
**Phase:** 1 — Corpus, retrieval, verifier
**PRs open:** #49 (#18), #50, #51, #53 (#21), #54 (#13), #55 (#14)
**PRs merged:** #52 (#19)
**CI:** #53/#54 green; #55's `reranker-fidelity` job (renamed from
`reranker-onnx`) blocked on a pending IAM grant, not a code issue (see
"Known open thread" below) — everything else in this document is either
merged, or open with its own CI green.

This is a progress snapshot, not a session handoff — nothing here says the
build is done. Read `CLAUDE.md` → `CONTEXT.md` → `DECISIONS.md` → `PHASES.md`
→ `SERVICES.md` → `BUILD_LOG.md` first; this document doesn't repeat them.

## Architecture

Hybrid graph RAG for compliance: check a proposal or document against law and
company policy, with every citation verbatim, versioned and clickable to the
clause. Two tracks meet at generation:

- **Track A (deterministic)** — a Neo4j graph of obligations, conditions and
  crosswalks. `GraphStore.applicable_obligations(profile, as_of, max_depth)`
  is bitemporal, depth-capped, and returns the full unranked set — no top-k,
  no similarity score standing in for a legal judgement (D-03, D-06).
- **Track B (retrieval)** — Qdrant hybrid search (dense + BM25 sparse, fused
  by native RRF), filtered to exactly the obligation IDs Track A already
  decided apply. `RetrievalStore.search(...)`. Only Track B's candidates go
  through the **reranker** (cross-encoder, second-stage scoring); Track A's
  obligations never enter a ranking pool — they filter Track B and stop
  there (D-03: "A deterministic finding must never lose a relevance contest
  to a topically similar paragraph").
- **Claude on Vertex** then generates an answer over both tracks, and a
  **CitationVerifier** deterministically re-checks every quoted span against
  canonical clause text before anything ships — a bad citation must block,
  never just get flagged (D-02).

Every external call goes through one of eight interfaces in
`adapters/interfaces.py`: `GraphStore`, `RetrievalStore`, `LedgerStore`,
`ClauseStore` (Cloud Storage, issue #15), `Reranker`, `Embedder`,
`Generator`, `CitationVerifier`. No Cypher, Qdrant, Vertex or SQL call
exists anywhere else in the codebase. Every interface has an in-memory fake
and a contract test proving fake and real satisfy the same behaviour.

## Components

| Component | What it does | Files | Status |
|---|---|---|---|
| Corpus models | Stable ID scheme, canonicalisation, tenant/instrument grammar | `corpus/models.py` | Done |
| Law parser (#17) | legislation.gov.au HTML → `ClauseRecord`s, anchor injection | `corpus/parser.py` | Merged |
| Policy parser (#20) | Synthetic Acme policies (Markdown+PDF) → clauses | `corpus/policy_parser.py` | Merged |
| Chunker (#6, #18) | Clause → chunk, 420-token budget derived from the reranker's own window | `corpus/chunker.py` | Merged |
| Cloud Storage ingest (#19) | Selection manifest → clause objects + `manifest.json`, drift-detected against silent re-ingest | `corpus/ingest.py`, `adapters/gcs_clause_store.py` | **Merged (PR #52)** — first real adapter, first live GCP write |
| Real-tokenizer budget test (#21) | Proves the 420-token cap against the actual vendored XLM-R tokenizer, not a stub | `corpus/reranker_tokenizer.py`, `vendor/bge-reranker-v2-m3-tokenizer/` | PR #53 open, CI green, awaiting merge |
| Vertex embedder (#13) | `gemini-embedding-001`, 1536 dims, one request per text, `auto_truncate=False` | `adapters/vertex_embedder.py` | PR #54 open, CI green — **blocked on a human go-ahead** for the first live smoke-test call (billable ask made in the PR, not yet actioned) |
| Reranker service (#14) | `BAAI/bge-reranker-v2-m3` → int8 ONNX (converted in-house, pinned to commit `953dc6f`), FastAPI + plain onnxruntime, Docker-local. Artifact converted once locally, uploaded hashed/versioned to the corpus bucket; CI fetches and re-checks fidelity, no conversion in CI | `scripts/export_reranker_onnx.py`, `scripts/upload_reranker_artifact.py`, `scripts/fetch_reranker_artifact.py`, `reranker_service/`, `adapters/reranker_client.py` | PR #55 open — CI blocked on an IAM grant, not code (see below) |
| Ledger (Postgres) | Assessments, tenant profiles, trace IDs | `db/ledger/` | Schema exists, tested in CI; no application code consumes it yet |
| Graph, real Retrieval (Qdrant), real Generator, real CitationVerifier | — | `adapters/fakes.py` only | Interfaces + fakes only, not started |
| Router (#5), retrieval pipeline wiring | Decide ID-lookup vs hybrid search, assemble `retrieve()` | — | Not started |

## What's next / what's blocked

- **#22 (Qdrant)** is the next high-risk, billable gate — stop and wait for
  explicit review before it starts, per the standing Lane instructions.
- **#13** needs the human's go-ahead for one live Vertex Embeddings call
  (a fraction of a cent) before its `BUILD_LOG.md` entry can be written and
  the PR considered fully done.
- Graph population, the router, and real Retrieval/Generator/Verifier
  adapters haven't been started at all — Phase 1's exit criteria aren't met
  yet even once #53/#54/#55 merge.

## Known open thread: PR #55's reranker CI job

Three consecutive attempts to convert the model inside CI's `reranker-onnx`
job failed at the same point in `quantize_dynamic()`. Root-caused from the
actual job logs (not guessed): one run surfaced the real error directly —
`OSError: [Errno 28] No space left on device`, raised inside onnxruntime's
own internal reload-with-external-data step, which writes yet another full
copy of the model's weights to disk mid-quantization, on top of the fp32
download, its own ONNX export, and the int8 output already there; the other
two were killed by the host ("the runner has received a shutdown signal")
at the identical point — consistent with the same disk exhaustion, not an
OOM kill (no exit 137 anywhere) and not three unrelated failures. This also
burned real money: the repo's GitHub Actions usage this month is over the
private-repo free tier, $1.47 metered overage to date (BUILD_LOG.md
2026-09-28) — the first non-zero line item in the project.

Resolution: conversion was moved out of CI entirely rather than chasing a
fourth fix. It now happens once, locally
(`scripts/export_reranker_onnx.py`), and the verified int8 artifact is
uploaded to the corpus bucket, hashed and version-locked
(`scripts/upload_reranker_artifact.py`; uploaded and round-tripped
2026-09-28, BUILD_LOG.md). CI's job was renamed `reranker-fidelity` and now
only fetches that artifact and re-checks its fp32-vs-int8 fidelity through
plain `onnxruntime` — no torch, no optimum, no 2.27GB download, ever again.

**Still blocked**: `reranker-fidelity` needs `compliance-rag-ci` to read
`gs://compliance-rag-508904-corpus/reranker/`, and that service account is
deliberately scoped to `logging.*` only (SERVICES.md). A narrow
`storage.objectViewer` grant, conditioned to the `reranker/` prefix, was
proposed to the human and is awaiting a pasted approval line — the job
cannot go green until it lands. The conversion itself remains verified
correct and reproducible locally (identical fp32/int8 scores and preserved
rank order across every local run; the actual Docker image was built and
run, and answered `POST /rerank` correctly against a live container).
