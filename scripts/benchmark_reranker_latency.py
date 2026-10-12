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
from urllib.parse import urlparse

import httpx

from compliance_rag.eval.golden import Category, load_golden_set
from compliance_rag.eval.harness import golden_store
from compliance_rag.eval.recall import WHOLE_LIBRARY, NoRerank
from compliance_rag.retrieval.pipeline import RetrievalPipeline


async def batches() -> list[tuple[str, tuple[str, ...]]]:
    """Every paraphrase and cross-clause query's fused top 50."""
    async with golden_store(os.environ["QDRANT_URL"], prefix="latency-bench") as store:
        pipeline = RetrievalPipeline(store, NoRerank())
        out: list[tuple[str, tuple[str, ...]]] = []
        for q in load_golden_set().queries:
            if q.category is Category.EXACT_ID:
                continue
            trace = await pipeline.retrieve(
                q.query, q.tenant_id, q.as_of, law=WHOLE_LIBRARY, limit=50
            )
            out.append((q.query, tuple(c.text for c in trace.fused)))
        return out


def sydney_run_url(url: str) -> str:
    """Only an https `*.a.run.app` host with the `-ts` (australia-southeast1)
    region code gets the bearer token — parsed, not substring-matched, so a
    lookalike path or host can't receive it."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith("-ts.a.run.app"):
        raise SystemExit("RERANKER_URL must be the australia-southeast1 Cloud Run service")
    return url


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
    url = sydney_run_url(os.environ["RERANKER_URL"])
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
