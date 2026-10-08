"""Reranker benchmark, latency half (issue #32): real request latency of the
deployed `reranker` Cloud Run service for 50- and 20-candidate batches.

The batches are real: each golden paraphrase / cross-clause query's own fused
candidates from the recall harness (#28), cut to 50 and to 20. Calls go
straight to the service with an ID token for `compliance-rag-runtime` — the
production caller — taken from `RERANKER_ID_TOKEN`, never printed:

    export RERANKER_ID_TOKEN=$(gcloud auth print-identity-token \\
        --impersonate-service-account=compliance-rag-runtime@<project>.iam.gserviceaccount.com \\
        --audiences=$RERANKER_URL)
    QDRANT_URL=... RERANKER_URL=... uv run python scripts/benchmark_reranker_latency.py warm
    # after >= 20 minutes with no traffic (the service's idle-to-zero window):
    QDRANT_URL=... RERANKER_URL=... uv run python scripts/benchmark_reranker_latency.py cold 20

`warm` sends one warm-up request, then each query's 50- and 20-candidate
batches alternately and reports median and max (with this few samples, max is
the honest p95). `cold N` sends exactly one N-candidate request and reports
it: cold start is measured on its own, never averaged into warm.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

import httpx
from qdrant_client import AsyncQdrantClient

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden import Category, load_golden_set
from compliance_rag.eval.golden_corpus import golden_chunks
from compliance_rag.eval.recall import NoRerank
from compliance_rag.retrieval.pipeline import RetrievalPipeline


async def batches() -> list[tuple[str, tuple[str, ...]]]:
    client = AsyncQdrantClient(url=os.environ["QDRANT_URL"])
    collection = f"latency-bench-{uuid.uuid4().hex[:8]}"
    store = QdrantRetrievalStore(
        client,
        collection_name=collection,
        embedder=CachedEmbedder(EmbeddingCache.load()),
        ledger=FakeLedgerStore(),
    )
    try:
        await store.ensure_ready()
        await store.upsert_chunks(golden_chunks())
        pipeline = RetrievalPipeline(store, NoRerank())
        out: list[tuple[str, tuple[str, ...]]] = []
        for q in load_golden_set().queries:
            if q.category is Category.EXACT_ID:
                continue
            trace = await pipeline.retrieve(q.query, q.tenant_id, q.as_of, limit=50)
            out.append((q.query, tuple(c.text for c in trace.fused)))
        return out
    finally:
        await client.delete_collection(collection)
        await client.close()


async def timed(http: httpx.AsyncClient, query: str, docs: tuple[str, ...]) -> float:
    start = time.perf_counter()
    response = await http.post(
        "/rerank", json={"query": query, "documents": list(docs), "top_k": 10}
    )
    response.raise_for_status()
    return time.perf_counter() - start


def stats(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "median_s": round(ordered[len(ordered) // 2], 2),
        "max_s": round(ordered[-1], 2),
        "min_s": round(ordered[0], 2),
    }


async def main(mode: str, size: int) -> None:
    url = os.environ["RERANKER_URL"]
    if not url.endswith(".a.run.app") or "-ts." not in url:
        raise SystemExit("RERANKER_URL must be the australia-southeast1 Cloud Run service")
    headers = {"Authorization": f"Bearer {os.environ['RERANKER_ID_TOKEN']}"}
    work = await batches()
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=180) as http:
        if mode == "cold":
            query, docs = work[0]
            seconds = await timed(http, query, docs[:size])
            print(
                json.dumps({"cold": {"candidates": len(docs[:size]), "seconds": round(seconds, 2)}})
            )
            return
        await timed(http, work[0][0], work[0][1][:5])  # warm-up, not counted
        fifty: list[float] = []
        twenty: list[float] = []
        for query, docs in work[:8]:
            fifty.append(await timed(http, query, docs[:50]))
            twenty.append(await timed(http, query, docs[:20]))
        sizes = sorted({len(d[:50]) for _, d in work[:8]})
        print(
            json.dumps({"warm_50": stats(fifty), "warm_20": stats(twenty), "batch_sizes_50": sizes})
        )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 50))
