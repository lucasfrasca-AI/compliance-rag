"""Reranker benchmark, quality half (issue #32): recall@10 per golden-set
category with no rerank, and with the deployed int8 cross-encoder over the top
50 and the top 20 fused candidates.

Same harness as the recall gate (#28) — the golden corpus on a real Qdrant,
real cached `gemini-embedding-001` vectors, local BM25 — with the reranker
service's own FastAPI app behind the real `RerankerClient` (in-process ASGI,
no network), so the cross-encoder arm is the production code path. Also
reports per-query rerank wall time on this machine and whether two identical
runs rank identically.

    QDRANT_URL=http://localhost:6333 uv run python scripts/benchmark_reranker_recall.py

`RERANKER_ONNX_DIR=models/candidates/<name>` swaps in a candidate model
exported by `scripts/export_candidate_reranker.py` (its own `model.onnx` and
tokenizer) — same service code, same thread pinning, same harness.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from qdrant_client import AsyncQdrantClient

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.interfaces import Reranker, ScoredDocument
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.reranker_client import RerankerClient
from compliance_rag.adapters.reranker_http_client import build_reranker_http_client
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden import Category, load_golden_set
from compliance_rag.eval.golden_corpus import golden_chunks
from compliance_rag.eval.recall import (
    FUSED,
    RERANKED,
    NoRerank,
    QueryRecall,
    by_category,
    measure_trace,
)
from compliance_rag.reranker_service.app import create_app
from compliance_rag.reranker_service.model import RerankerModel
from compliance_rag.retrieval.pipeline import RetrievalPipeline


class Timed(Reranker):
    def __init__(self, inner: Reranker) -> None:
        self.inner = inner
        self.seconds: list[float] = []

    async def rerank(
        self, query: str, documents: tuple[str, ...], *, top_k: int
    ) -> tuple[ScoredDocument, ...]:
        start = time.perf_counter()
        result = await self.inner.rerank(query, documents, top_k=top_k)
        self.seconds.append(time.perf_counter() - start)
        return result


@dataclass
class Arm:
    name: str
    candidates: int
    reranker: Timed
    gated: str


async def run_arm(
    store: QdrantRetrievalStore, arm: Arm
) -> tuple[dict[str, object], list[list[str]]]:
    golden = load_golden_set()
    pipeline = RetrievalPipeline(store, arm.reranker)
    results: list[QueryRecall] = []
    rankings: list[list[str]] = []
    for q in golden.queries:
        trace = await pipeline.route(q.query, q.tenant_id, q.as_of, limit=arm.candidates, top_k=10)
        results.append(measure_trace(q, trace))
        rankings.append([c.clause_id for c in trace.ranked_candidates()])
    summary = by_category(results)
    per_category = {c.value: round(summary[c][arm.gated], 3) for c in Category}
    misses = {
        r.query_id: round(r.by_list[arm.gated], 3) for r in results if r.by_list[arm.gated] < 1
    }
    times = sorted(arm.reranker.seconds)
    timing = (
        {
            "queries": len(times),
            "median_s": round(times[len(times) // 2], 2),
            "max_s": round(times[-1], 2),
        }
        if times
        else {}
    )
    return {
        "arm": arm.name,
        "recall_at_10": per_category,
        "misses": misses,
        "local_rerank": timing,
    }, rankings


async def main() -> None:
    client = AsyncQdrantClient(url=os.environ["QDRANT_URL"])
    collection = f"reranker-bench-{uuid.uuid4().hex[:8]}"
    store = QdrantRetrievalStore(
        client,
        collection_name=collection,
        embedder=CachedEmbedder(EmbeddingCache.load()),
        ledger=FakeLedgerStore(),
    )
    candidate = os.environ.get("RERANKER_ONNX_DIR")
    model = (
        RerankerModel(onnx_path=Path(candidate) / "model.onnx", tokenizer_dir=Path(candidate))
        if candidate
        else RerankerModel()
    )
    print(json.dumps({"model": candidate or "production bge-reranker-v2-m3 int8"}), flush=True)
    app = create_app(model)

    def cross_encoder() -> Timed:
        http = build_reranker_http_client(
            "http://localhost",
            timeout_seconds=600,
            transport=httpx.ASGITransport(app=app),
        )
        return Timed(RerankerClient(http))

    try:
        await store.ensure_ready()
        await store.upsert_chunks(golden_chunks())
        arms = [
            Arm("no-rerank (fused, top 50 candidates)", 50, Timed(NoRerank()), FUSED),
            Arm("cross-encoder over top 50", 50, cross_encoder(), RERANKED),
            Arm("cross-encoder over top 20", 20, cross_encoder(), RERANKED),
        ]
        report: list[dict[str, object]] = []
        rankings: dict[str, list[list[str]]] = {}
        for arm in arms:
            row, ranked = await run_arm(store, arm)
            report.append(row)
            rankings[arm.name] = ranked
            print(json.dumps(row), flush=True)
        # Determinism: the same arm twice on the same corpus.
        _, ranked_again = await run_arm(store, Arm("repeat 50", 50, cross_encoder(), RERANKED))
        same = sum(
            a == b for a, b in zip(rankings["cross-encoder over top 50"], ranked_again, strict=True)
        )
        print(
            json.dumps(
                {"determinism_top50": f"{same}/{len(ranked_again)} queries ranked identically"}
            )
        )
    finally:
        await client.delete_collection(collection)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
